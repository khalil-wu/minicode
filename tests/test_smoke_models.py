import asyncio
import json
import json as jsonlib
from collections.abc import AsyncIterator
from types import SimpleNamespace
from fastapi.testclient import TestClient

from backend.agent.loop import run_agent_loop
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.attachments.store import AttachmentStore
from backend.config import PROJECT_ROOT, AgentSettings, LLMSettings, PermissionSettings, TokenBudget, load_llm_settings
from backend.llm.base import LLMAdapter, LLMMessage, StreamEvent, StreamEventType, ToolCallEvent
from backend.tools.agent_tools import ReadArtifactTool
from backend.llm.errors import classify_llm_error
from backend.llm.openai_adapter import OpenAIAdapter, _clean_error_message
from backend.agent.message import AgentEvent
from backend.conversations.repository import ConversationRepository
from backend.main import app
from backend.services.llm_provider_helpers import _build_openai_models_url, _extract_model_ids
from backend.permissions.checker import PermissionChecker
from backend.agent.tool_execution import generate_diff as _generate_diff
from backend.tools.base import BaseTool, PermissionLevel, ToolResult
from backend.tools.registry import ToolRegistry
from backend.ws.handler import _build_effective_user_message


async def _raising_fetch_models(*args, **kwargs):
    raise RuntimeError("upstream blocked")


class _ProxyLimitLLM(LLMAdapter):
    def __init__(self) -> None:
        self.simple_chat_calls = 0

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        yield StreamEvent(
            type=StreamEventType.ERROR,
            content="Concurrency limit exceeded for account, please retry later",
        )

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        self.simple_chat_calls += 1
        return "fallback should not run"


class _BackupSuccessLLM(LLMAdapter):
    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="backup reply")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        return "backup reply"


class _RetryThenSuccessLLM(LLMAdapter):
    def __init__(self) -> None:
        self.stream_chat_calls = 0
        self.simple_chat_calls = 0

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.stream_chat_calls += 1
        if self.stream_chat_calls == 1:
            yield StreamEvent(
                type=StreamEventType.ERROR,
                content="LLM 流式响应异常: Concurrency limit exceeded for account, please retry later",
            )
            return
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="retry recovered")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        self.simple_chat_calls += 1
        return "simple fallback should not run"


class _BillingErrorLLM(LLMAdapter):
    def __init__(self) -> None:
        self.stream_chat_calls = 0
        self.simple_chat_calls = 0

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.stream_chat_calls += 1
        yield StreamEvent(
            type=StreamEventType.ERROR,
            content="402 Payment Required: insufficient balance",
        )

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        self.simple_chat_calls += 1
        return "billing fallback should not run"


class _RetryableResponsesCreate:
    def __init__(self, *, stream_failures: int = 0, simple_failures: int = 0) -> None:
        self.calls = 0
        self.kwargs: list[dict] = []
        self._stream_failures = stream_failures
        self._simple_failures = simple_failures

    async def create(self, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
        if kwargs.get("stream"):
            if self._simple_failures > 0:
                self._simple_failures -= 1
                raise RuntimeError("Concurrency limit exceeded for account, please retry later")
            if self._stream_failures > 0:
                self._stream_failures -= 1
                raise RuntimeError("Concurrency limit exceeded for account, please retry later")
            return _AsyncListStream(
                [
                    SimpleNamespace(type="response.output_text.delta", delta="retry ok"),
                    SimpleNamespace(
                        type="response.completed",
                        response=SimpleNamespace(
                            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                            output_text="retry ok",
                            output=[],
                        ),
                    ),
                ]
            )

        if self._simple_failures > 0:
            self._simple_failures -= 1
            raise RuntimeError("Concurrency limit exceeded for account, please retry later")

        return SimpleNamespace(output_text="retry simple ok", output=[])


class _RetryableResponsesClient:
    def __init__(self, *, stream_failures: int = 0, simple_failures: int = 0) -> None:
        self.responses = _RetryableResponsesCreate(
            stream_failures=stream_failures,
            simple_failures=simple_failures,
        )
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=None))


class _RawHTTPResponse:
    status_code = 200
    headers = {}

    def __init__(self, *, lines=None, payload=None):
        self._lines = list(lines or [])
        self._payload = payload

    async def aread(self):
        return b""

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aclose(self):
        return None


class _RawHTTPContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _jsonable(value):
    if isinstance(value, SimpleNamespace):
        return {key: _jsonable(item) for key, item in vars(value).items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


class _SDKRawHTTPClient:
    def __init__(self, sdk_client):
        self.sdk_client = sdk_client
        self.requests = []

    def stream(self, method, url, *, headers, json, timeout=None):
        del method, headers, timeout
        self.requests.append(dict(json))
        if "/images/generations" in url:
            async def image_response():
                return await self.sdk_client.images.generate(**json)
            class _ImageContext:
                async def __aenter__(_self):
                    result = await image_response()
                    payload = _jsonable(result)
                    return _RawHTTPResponse(payload=payload)
                async def __aexit__(_self, exc_type, exc, tb):
                    return False
            return _ImageContext()
        endpoint = (
            getattr(getattr(self.sdk_client, "chat", None), "completions", None)
            if "/chat/completions" in url
            else getattr(self.sdk_client, "responses", None)
        )
        async def build():
            result = await endpoint.create(**json)
            if hasattr(result, "__aiter__"):
                events = [_jsonable(item) async for item in result]
                return _RawHTTPResponse(
                    lines=[f"data: {jsonlib.dumps(item)}" for item in events] + ["data: [DONE]"]
                )
            return _RawHTTPResponse(payload=_jsonable(result))
        class _Context:
            async def __aenter__(_self):
                return await build()
            async def __aexit__(_self, exc_type, exc, tb):
                return False
        return _Context()


class _ImageResponsesCreate:
    def __init__(self) -> None:
        self.calls = 0
        self.kwargs: list[dict] = []

    async def create(self, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
        return _AsyncListStream(
            [
                SimpleNamespace(
                    type="response.completed",
                    response=SimpleNamespace(
                        usage=SimpleNamespace(input_tokens=2, output_tokens=1),
                        output=[
                            SimpleNamespace(
                                type="image_generation_call",
                                result="iVBORw0KGgo=",
                            )
                        ],
                    ),
                )
            ]
        )


class _ImageResponsesClient:
    def __init__(self) -> None:
        self.responses = _ImageResponsesCreate()
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=None))
        self.images = _ImageGenerationCreate()


class _ImageGenerationCreate:
    def __init__(self) -> None:
        self.calls = 0
        self.kwargs: list[dict] = []

    async def generate(self, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
        return SimpleNamespace(
            data=[
                SimpleNamespace(
                    b64_json=(
                        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
                        "AAAAC0lEQVR42mP8/x8AAusB9Y9ZPhwAAAAASUVORK5CYII="
                    )
                )
            ]
        )


class _ToolSchemaRetryChatCompletions:
    def __init__(self) -> None:
        self.calls = 0
        self.seen_tools: list[bool] = []

    async def create(self, **kwargs):
        self.calls += 1
        self.seen_tools.append(bool(kwargs.get("tools")))
        if kwargs.get("tools"):
            raise RuntimeError("400 Bad Request: invalid tools[0].function.parameters schema")
        return _AsyncListStream(
            [
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content="tool-free reply", tool_calls=None),
                            finish_reason=None,
                        )
                    ],
                    usage=None,
                ),
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content=None, tool_calls=None),
                            finish_reason="stop",
                        )
                    ],
                    usage=None,
                ),
            ]
        )


class _ToolSchemaRetryChatClient:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=_ToolSchemaRetryChatCompletions())
        self.responses = SimpleNamespace(create=None)


class _BlockedGatewayError(RuntimeError):
    status_code = 403


class _ProviderHTTPError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: str = "",
        code: str = "",
        error_type: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.type = error_type
        self.response = SimpleNamespace(
            status_code=status_code,
            text=body,
            content=body.encode("utf-8"),
        )


class _BlockedToolRetryChatCompletions(_ToolSchemaRetryChatCompletions):
    async def create(self, **kwargs):
        self.calls += 1
        self.seen_tools.append(bool(kwargs.get("tools")))
        if kwargs.get("tools"):
            raise _BlockedGatewayError("Your request was blocked.")
        return _AsyncListStream(
            [
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content="tool-free reply", tool_calls=None),
                            finish_reason=None,
                        )
                    ],
                    usage=None,
                ),
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content=None, tool_calls=None),
                            finish_reason="stop",
                        )
                    ],
                    usage=None,
                ),
            ]
        )


class _BlockedToolRetryChatClient:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=_BlockedToolRetryChatCompletions())
        self.responses = SimpleNamespace(create=None)


class _AsyncListStream:
    def __init__(self, items: list[object]) -> None:
        self._items = items

    def __aiter__(self):
        self._iterator = iter(self._items)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _FakeChatHTTPStreamResponse:
    status_code = 200

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aread(self):
        return b""

    def raise_for_status(self) -> None:
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeChatHTTPStreamContext:
    def __init__(self, response: _FakeChatHTTPStreamResponse) -> None:
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeChatHTTPClient:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.requests: list[dict[str, object]] = []

    def stream(self, method, url, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        return _FakeChatHTTPStreamContext(_FakeChatHTTPStreamResponse(self.lines))


class _RealtimeSearchErrorLLM(LLMAdapter):
    def __init__(self) -> None:
        self.simple_chat_calls = 0

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        yield StreamEvent(
            type=StreamEventType.ERROR,
            content="Connection error.",
        )

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        self.simple_chat_calls += 1
        return "我这边联网搜索工具当前不可用，无法可靠获取北京今天的实时天气。"


class _FakeWebSearchTool(BaseTool):
    name = "mcp__websearch__search"
    description = "Search web"
    read_only = True

    def get_schema(self) -> object:
        from backend.tools.base import ToolSchema

        return ToolSchema(
            name=self.name,
            description="搜索互联网实时信息",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
                "required": ["query"],
            },
        )

    async def execute(self, args: dict[str, object]) -> object:
        from backend.tools.base import ToolResult

        return ToolResult(content="北京天气：小雨，11°C。")


class _RealtimeSearchNeedsToolLLM(LLMAdapter):
    def __init__(self) -> None:
        self.stream_chat_calls = 0
        self.saw_tool_result = False

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.stream_chat_calls += 1
        self.saw_tool_result = any(msg.role == "tool" for msg in messages)

        if self.saw_tool_result:
            yield StreamEvent(
                type=StreamEventType.TEXT_CHUNK,
                content="根据搜索结果，北京今天多云，12°C 到 22°C。",
            )
            yield StreamEvent(type=StreamEventType.DONE)
            return

        yield StreamEvent(
            type=StreamEventType.TEXT_CHUNK,
            content="请告诉我你想让我做什么。",
        )
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        return "tool-less fallback should not run"


class _RealtimeSearchGroundingLLM(LLMAdapter):
    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        system_text = "\n".join(
            msg.content for msg in messages if msg.role == "system"
        )

        if "绝不能声称没有搜索工具" in system_text:
            yield StreamEvent(
                type=StreamEventType.TEXT_CHUNK,
                content="根据现有搜索结果，北京今天有小雨，当前约 11°C。",
            )
            yield StreamEvent(type=StreamEventType.DONE)
            return

        yield StreamEvent(
            type=StreamEventType.TEXT_CHUNK,
            content="抱歉，这个会话里没有搜索工具，我无法查询北京今天天气。",
        )
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        return "tool-less fallback should not run"


class _RealtimeWeatherPreambleLLM(LLMAdapter):
    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        yield StreamEvent(
            type=StreamEventType.TEXT_CHUNK,
            content="我先查一下北京今天的实时天气。",
        )
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        return "tool-less fallback should not run"


class _ClarifyThenAnswerWeatherLLM(LLMAdapter):
    def __init__(self) -> None:
        self.calls = 0

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="北京今天多云，8°C 到 18°C。")
            yield StreamEvent(type=StreamEventType.DONE)
            return

        assert any(
            "city/location first" in message.content or "missing a city or area" in message.content
            for message in messages
        )
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="你想查哪个城市的天气？")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        return "你想查哪个城市的天气？"


class _RecordingWebSearchTool(_FakeWebSearchTool):
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def execute(self, args: dict[str, object]) -> object:
        from backend.tools.base import ToolResult

        self.calls.append(dict(args))
        return ToolResult(content="北京天气：多云，12°C 到 22°C。")


class _SearchResultsWebSearchTool(_FakeWebSearchTool):
    async def execute(self, args: dict[str, object]) -> object:
        from backend.tools.base import ToolResult

        return ToolResult(
            content=(
                "搜索 \"北京今天天气怎么样\" 的结果：\n\n"
                "1. **北京天气**\n"
                "   https://weather.example/beijing\n"
                "   北京天气实时详情\n"
            )
        )


class _RecordingFetchPageTool(BaseTool):
    name = "mcp__websearch__fetch_page"
    description = "Fetch page"
    read_only = True

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def get_schema(self) -> object:
        from backend.tools.base import ToolSchema

        return ToolSchema(
            name=self.name,
            description="获取网页正文",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                },
                "required": ["url"],
            },
        )

    async def execute(self, args: dict[str, object]) -> object:
        from backend.tools.base import ToolResult

        self.calls.append(dict(args))
        return ToolResult(content="## 北京天气\n来源: https://weather.example/beijing\n\n小雨\n\n11℃\n")


async def _fake_sleep(*args, **kwargs):
    pass


async def _collect_events(stream):
    return [event async for event in stream]


def _final_text(events) -> str:
    return "".join(
        str(event.data["item"].get("text") or "")
        for event in events
        if event.type == "item.completed"
        and isinstance(event.data.get("item"), dict)
        and event.data["item"].get("type") == "agent_message"
    )


def _final_events(events):
    return [
        event
        for event in events
        if event.type == "item.completed"
        and isinstance(event.data.get("item"), dict)
        and event.data["item"].get("type") == "agent_message"
    ]


async def _collect_stream_events(stream):
    return [event async for event in stream]


def test_refresh_models_uses_lucen_preset_when_live_discovery_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.api.routes_llm._fetch_openai_compatible_models",
        _raising_fetch_models,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/llm/models/refresh",
            json={
                "provider": "openai",
                "openai": {
                    "api_key": "test-key",
                    "base_url": "https://lucen.cc/v1",
                    "model": "gpt-5.4",
                },
                "anthropic": {},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider_id"] == "openai"
    assert payload["source"] == "manual"
    assert payload["models"] == ["gpt-5.4"]


def test_refresh_models_keeps_current_model_for_custom_gateway_without_presets(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.api.routes_llm._fetch_openai_compatible_models",
        _raising_fetch_models,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/llm/models/refresh",
            json={
                "provider": "openai",
                "openai": {
                    "api_key": "test-key",
                    "base_url": "https://gateway.example/v1",
                    "model": "custom-model-a",
                },
                "anthropic": {},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider_id"] == "openai"
    assert payload["source"] == "manual"
    assert payload["models"] == ["custom-model-a"]


def test_refresh_models_does_not_persist_fallback_models_when_live_discovery_fails(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.config_helpers.SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(
        "backend.api.routes_llm._fetch_openai_compatible_models",
        _raising_fetch_models,
    )

    from backend.config import save_llm_settings

    save_llm_settings(
        {
            "provider": "custom",
            "custom": {
                "api_key": "saved-custom-key",
                "base_url": "https://gateway.example/v1",
                "model": "custom-model-a",
                "available_models": ["custom-model-a"],
            },
        }
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/llm/models/refresh",
            json={
                "provider": "custom",
                "openai": {},
                "anthropic": {},
                "custom": {
                    "api_key": "",
                    "base_url": "https://gateway.example/v1",
                    "model": "custom-model-a",
                    "available_models": ["custom-model-a"],
                },
            },
        )
        settings_response = client.get("/api/llm/settings")

    assert response.status_code == 200
    assert response.json()["source"] == "manual"
    saved = settings_response.json()
    assert saved["custom"]["available_models"] == ["custom-model-a"]


def test_refresh_models_preserves_manual_custom_gateway_models_when_live_discovery_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.api.routes_llm._fetch_openai_compatible_models",
        _raising_fetch_models,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/llm/models/refresh",
            json={
                "provider": "openai",
                "openai": {
                    "api_key": "test-key",
                    "base_url": "https://gateway.example/v1",
                    "model": "custom-model-b",
                    "available_models": ["custom-model-a", "custom-model-b"],
                },
                "anthropic": {},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider_id"] == "openai"
    assert payload["source"] == "manual"
    assert payload["models"] == ["custom-model-a", "custom-model-b"]
    assert payload["failure_kind"] == "model_discovery_failed"
    assert payload["retryable"] is False
    assert payload["source_message"] == payload["message"]
    assert "hint" in payload


def test_refresh_models_keeps_a_configured_model_live_discovery_no_longer_lists(monkeypatch) -> None:
    async def _live_models(*args, **kwargs):
        return ["gpt-5.4-mini", "gpt-5.4"]

    monkeypatch.setattr(
        "backend.api.routes_llm._fetch_openai_compatible_models",
        _live_models,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/llm/models/refresh",
            json={
                "provider": "openai",
                "openai": {
                    "api_key": "test-key",
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-4o",
                },
                "anthropic": {},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "live"
    # An explicitly configured model is a user choice: it stays selected and
    # visible even when discovery stops advertising it, and the adapter boundary
    # reports the mismatch instead of switching models silently.
    assert payload["models"] == ["gpt-5.4-mini", "gpt-5.4", "gpt-4o"]
    assert payload["selected_model"] == "gpt-4o"


def test_refresh_models_keeps_current_custom_model_when_live_discovery_omits_it(monkeypatch) -> None:
    async def _live_models(*args, **kwargs):
        return ["gpt-5.4-mini"]

    monkeypatch.setattr(
        "backend.api.routes_llm._fetch_openai_compatible_models",
        _live_models,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/llm/models/refresh",
            json={
                "provider": "custom",
                "openai": {},
                "anthropic": {},
                "custom": {
                    "api_key": "test-key",
                    "base_url": "https://gateway.example/v1",
                    "model": "gpt-5.4-mini",
                    "available_models": [],
                },
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "custom"
    assert payload["provider_id"] == "custom"
    assert payload["source"] == "live"
    assert payload["models"][0] == "gpt-5.4-mini"
    assert payload["models"] == ["gpt-5.4-mini"]


def test_refresh_models_uses_saved_custom_key_and_persists_live_models(monkeypatch, tmp_path) -> None:
    seen: dict[str, str] = {}

    async def _live_models(base_url: str, api_key: str, **_transport):
        seen["base_url"] = base_url
        seen["api_key"] = api_key
        return ["claude-opus-4-6", "claude-sonnet-4-6"]

    monkeypatch.setattr("backend.config_helpers.SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr("backend.api.routes_llm._fetch_openai_compatible_models", _live_models)

    from backend.config import save_llm_settings

    save_llm_settings(
        {
            "provider": "custom",
            "custom": {
                "api_key": "saved-custom-key",
                "base_url": "https://gateway.example/v1",
                "model": "claude-opus-4-6",
                "available_models": ["stale-model"],
            },
        }
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/llm/models/refresh",
            json={
                "provider": "custom",
                "openai": {},
                "anthropic": {},
                "custom": {
                    "api_key": "",
                    "base_url": "https://gateway.example/v1",
                    "model": "claude-opus-4-6",
                    "available_models": ["claude-opus-4-6"],
                },
            },
        )
        settings_response = client.get("/api/llm/settings")

    assert response.status_code == 200
    payload = response.json()
    assert seen == {
        "base_url": "https://gateway.example/v1",
        "api_key": "saved-custom-key",
    }
    assert payload["source"] == "live"
    assert payload["models"] == ["claude-opus-4-6", "claude-sonnet-4-6"]

    saved = settings_response.json()
    assert saved["custom"]["available_models"] == ["claude-opus-4-6", "claude-sonnet-4-6"]


def test_refresh_models_uses_anthropic_discovery_for_custom_anthropic_wire_api(monkeypatch, tmp_path) -> None:
    seen: dict[str, str] = {}

    async def _anthropic_models(base_url: str, api_key: str, **_transport):
        seen["base_url"] = base_url
        seen["api_key"] = api_key
        return ["claude-opus-4-6", "claude-sonnet-4-6"]

    async def _openai_models(*args, **kwargs):
        raise AssertionError("custom anthropic refresh should not use OpenAI /models")

    monkeypatch.setattr("backend.config_helpers.SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr("backend.api.routes_llm._fetch_anthropic_models", _anthropic_models)
    monkeypatch.setattr("backend.api.routes_llm._fetch_openai_compatible_models", _openai_models)

    from backend.config import save_llm_settings

    save_llm_settings(
        {
            "provider": "custom",
            "custom": {
                "api_key": "saved-custom-key",
                "base_url": "https://gateway.example",
                "model": "claude-opus-4-6",
                "wire_api": "anthropic",
            },
        }
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/llm/models/refresh",
            json={
                "provider": "custom",
                "openai": {},
                "anthropic": {},
                "custom": {
                    "api_key": "",
                    "base_url": "https://gateway.example",
                    "model": "claude-opus-4-6",
                    "wire_api": "anthropic",
                },
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert seen == {
        "base_url": "https://gateway.example",
        "api_key": "saved-custom-key",
    }
    assert payload["provider_id"] == "custom_anthropic"
    assert payload["source"] == "live"
    assert payload["models"] == ["claude-opus-4-6", "claude-sonnet-4-6"]
    assert payload["selected_model"] == "claude-opus-4-6"


def test_refresh_models_replaces_stale_openai_model_for_custom_anthropic(monkeypatch, tmp_path) -> None:
    async def _anthropic_models(base_url: str, api_key: str, **_transport):
        return ["claude-opus-4-6", "claude-sonnet-4-6"]

    monkeypatch.setattr("backend.config_helpers.SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr("backend.api.routes_llm._fetch_anthropic_models", _anthropic_models)

    from backend.config import get_custom_settings, save_llm_settings

    save_llm_settings(
        {
            "provider": "custom",
            "custom": {
                "api_key": "saved-custom-key",
                "base_url": "https://gateway.example",
                "model": "gpt-5.4-mini",
                "available_models": ["gpt-5.4-mini"],
                "wire_api": "anthropic",
            },
        }
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/llm/models/refresh",
            json={
                "provider": "custom",
                "custom": {
                    "base_url": "https://gateway.example",
                    "model": "gpt-5.4-mini",
                    "available_models": ["gpt-5.4-mini"],
                    "wire_api": "anthropic",
                },
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider_id"] == "custom_anthropic"
    # Same contract as the openai path: the configured model is preserved and
    # merged into the discovered list rather than replaced by discovery.
    assert payload["selected_model"] == "gpt-5.4-mini"
    assert payload["models"] == ["claude-opus-4-6", "claude-sonnet-4-6", "gpt-5.4-mini"]

    saved = get_custom_settings()
    assert saved["model"] == "gpt-5.4-mini"
    assert saved["available_models"] == [
        "claude-opus-4-6",
        "claude-sonnet-4-6",
        "gpt-5.4-mini",
    ]


def test_save_custom_messages_preserves_provider_model_id(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.config_helpers.SETTINGS_FILE", tmp_path / "settings.json")

    from backend.config import get_custom_settings, save_llm_settings

    save_llm_settings(
        {
            "provider": "custom",
            "custom": {
                "api_key": "saved-custom-key",
                "base_url": "https://gateway.example",
                "model": "gpt-5.4-mini",
                "available_models": ["gpt-5.4-mini", "claude-sonnet-4-6"],
                "wire_api": "anthropic",
            },
        }
    )

    saved = get_custom_settings()
    # The Messages wire protocol does not imply Anthropic model ownership.
    # Compatible gateways may expose GPT or vendor-specific model ids.
    assert saved["model"] == "gpt-5.4-mini"
    assert saved["available_models"] == ["gpt-5.4-mini", "claude-sonnet-4-6"]


def test_extract_model_ids_sorts_live_models_by_provider_created_time() -> None:
    payload = {
        "data": [
            {"id": "gpt-4o", "created": 1715367049},
            {"id": "gpt-5.4", "created": 1780000000},
            {"id": "text-embedding-3-large", "created": 1715367049},
            {"id": "gpt-5.4-mini", "created": 1780000100},
        ]
    }

    assert _extract_model_ids(payload) == [
        "gpt-5.4-mini",
        "gpt-5.4",
        "gpt-4o",
        "text-embedding-3-large",
    ]


def test_openai_model_refresh_normalizes_provider_base_url_to_models_endpoint() -> None:
    assert _build_openai_models_url("https://api.openai.com") == "https://api.openai.com/v1/models"
    assert _build_openai_models_url("https://api.openai.com/v1/") == "https://api.openai.com/v1/models"
    assert _build_openai_models_url("https://openrouter.ai/api/v1") == "https://openrouter.ai/api/v1/models"


def test_openai_defaults_use_official_base_url_without_inventing_a_model(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.setattr("backend.config_helpers.SETTINGS_FILE", tmp_path / "settings.json")

    settings = load_llm_settings()

    assert settings.base_url == "https://api.openai.com/v1"
    assert settings.model == ""


def test_env_example_uses_official_openai_url_without_inventing_models() -> None:
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "OPENAI_BASE_URL=https://api.openai.com/v1" in env_example
    assert "OPENAI_MODEL=\n" in env_example
    assert "OPENAI_REASONING_EFFORT=\n" in env_example
    assert "ANTHROPIC_MODEL=\n" in env_example


def test_run_agent_loop_surfaces_proxy_limit_error_without_simple_chat_retry() -> None:
    llm = _ProxyLimitLLM()

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="hello",
                llm=llm,
                tool_registry=ToolRegistry(),
                artifact_store=ArtifactStore(),
                permission_checker=PermissionChecker(PermissionSettings()),
                agent_settings=AgentSettings(
                    max_iterations=1,
                    # This case asserts immediate surfacing; the production
                    # default intentionally retries transient busy responses.
                    stream_max_attempts=0,
                    stream_retry_delay_seconds=0,
                ),
                token_budget=TokenBudget(),
            )
        )
    )

    error_events = [event for event in events if event.type == "error"]
    assert error_events
    assert "繁忙" in error_events[0].data["message"]
    assert error_events[0].data["provider_error_type"] == "busy"
    assert llm.simple_chat_calls == 0



def test_run_agent_loop_retries_proxy_limit_before_surface(monkeypatch) -> None:
    monkeypatch.setattr("backend.agent.loop_runtime_helpers.asyncio.sleep", _fake_sleep)
    llm = _RetryThenSuccessLLM()

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="hello",
                llm=llm,
                tool_registry=ToolRegistry(),
                artifact_store=ArtifactStore(),
                permission_checker=PermissionChecker(PermissionSettings()),
                agent_settings=AgentSettings(max_iterations=1),
                token_budget=TokenBudget(),
            )
        )
    )

    error_events = [event for event in events if event.type == "error"]
    final_events = _final_events(events)

    assert error_events == []
    assert "".join(str(event.data["item"]["text"]) for event in final_events) == "retry recovered"
    assert llm.stream_chat_calls == 2
    assert llm.simple_chat_calls == 0


def test_run_agent_loop_treats_billing_error_as_fatal() -> None:
    llm = _BillingErrorLLM()

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="hello",
                llm=llm,
                tool_registry=ToolRegistry(),
                artifact_store=ArtifactStore(),
                permission_checker=PermissionChecker(PermissionSettings()),
                agent_settings=AgentSettings(max_iterations=1),
                token_budget=TokenBudget(),
            )
        )
    )

    error_events = [event for event in events if event.type == "error"]
    assert error_events
    assert error_events[0].data["recoverable"] is False
    assert error_events[0].data["error_type"] == "billing"
    assert llm.stream_chat_calls == 1
    assert llm.simple_chat_calls == 0


def test_classify_llm_error_extracts_provider_http_details_from_cause() -> None:
    cases = [
        (
            _ProviderHTTPError("401 Unauthorized", status_code=401, body='{"error":{"type":"auth_error"}}'),
            "auth",
            True,
            False,
        ),
        (
            _ProviderHTTPError("402 Payment Required", status_code=402, body='{"error":{"code":"insufficient_balance"}}'),
            "billing",
            True,
            False,
        ),
        (
            _ProviderHTTPError("403 Forbidden", status_code=403, body="Your request was blocked."),
            "blocked",
            True,
            False,
        ),
        (
            _ProviderHTTPError("407 Proxy Authentication Required", status_code=407),
            "proxy",
            True,
            False,
        ),
        (
            _ProviderHTTPError("429 Too Many Requests", status_code=429),
            "rate_limit",
            False,
            True,
        ),
        (
            _ProviderHTTPError("502 Bad Gateway", status_code=502),
            "network",
            False,
            True,
        ),
        (
            _ProviderHTTPError(
                "400 Bad Request",
                status_code=400,
                body='{"error":{"code":"model_not_found","message":"model deepseek-v4 does not exist"}}',
            ),
            "model",
            True,
            False,
        ),
        (
            _ProviderHTTPError(
                "404 Not Found",
                status_code=404,
                body='{"error":{"type":"invalid_request_error","message":"invalid model"}}',
            ),
            "model",
            True,
            False,
        ),
    ]

    for provider_exc, provider_type, fatal, retryable in cases:
        try:
            raise provider_exc
        except _ProviderHTTPError as cause:
            try:
                raise RuntimeError("LLM call failed") from cause
            except RuntimeError as wrapped:
                classification = classify_llm_error(wrapped)

        assert classification.provider_error_type == provider_type
        assert classification.fatal is fatal
        assert classification.retryable is retryable


def test_classify_llm_error_keeps_unknown_as_true_fallback() -> None:
    classification = classify_llm_error(RuntimeError("unexpected adapter failure"))

    assert classification.provider_error_type == "unknown"
    assert classification.fatal is False
    assert classification.retryable is False


def test_openai_adapter_surfaces_retryable_responses_stream_error_to_agent_loop() -> None:
    client = _RetryableResponsesClient(stream_failures=1)
    adapter = OpenAIAdapter(
        LLMSettings(api_key="test-key", model="gpt-5.4", wire_api="responses"),
        http_client=_SDKRawHTTPClient(client),
    )

    events = asyncio.run(
        _collect_stream_events(
            adapter.stream_chat([LLMMessage(role="user", content="hello")])
        )
    )

    assert [event.type for event in events] == [StreamEventType.ERROR]
    assert events[0].raw["provider_error_type"] == "busy"
    assert client.responses.calls == 1


def test_openai_responses_stream_emits_nonfinal_tool_call_when_arguments_done() -> None:
    class _ResponsesToolCallCreate:
        async def create(self, **kwargs):
            return _AsyncListStream(
                [
                    SimpleNamespace(
                        type="response.function_call_arguments.done",
                        call_id="call_pwd",
                        name="run_command",
                        arguments='{"command":"pwd"}',
                    ),
                    SimpleNamespace(
                        type="response.completed",
                        response=SimpleNamespace(
                            status="completed",
                            usage=SimpleNamespace(input_tokens=2, output_tokens=3),
                            output=[
                                SimpleNamespace(
                                    type="function_call",
                                    id="fc_pwd",
                                    call_id="call_pwd",
                                    name="run_command",
                                    arguments='{"command":"pwd"}',
                                    status="completed",
                                )
                            ],
                        ),
                    ),
                ]
            )

    client = SimpleNamespace(
        responses=_ResponsesToolCallCreate(),
        chat=SimpleNamespace(completions=SimpleNamespace(create=None)),
    )
    adapter = OpenAIAdapter(
        LLMSettings(api_key="test-key", model="gpt-5.4", wire_api="responses"),
        http_client=_SDKRawHTTPClient(client),
    )

    events = asyncio.run(
        _collect_stream_events(
            adapter.stream_chat(
                [LLMMessage(role="user", content="where am I")],
                tools=[{
                    "type": "function",
                    "function": {
                        "name": "run_command",
                        "description": "Run command",
                        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
                    },
                }],
            )
        )
    )

    tool_events = [event for event in events if event.type == StreamEventType.TOOL_CALL]
    assert [event.tool_calls_final for event in tool_events] == [False, True]
    assert tool_events[0].tool_calls == [
        ToolCallEvent(id="call_pwd", name="run_command", arguments={"command": "pwd"})
    ]
    assert tool_events[1].tool_calls == tool_events[0].tool_calls
    done = next(event for event in events if event.type == StreamEventType.DONE)
    assert done.usage.input_tokens == 2
    assert done.usage.output_tokens == 3


def test_openai_adapter_surfaces_retryable_simple_chat_error_to_caller() -> None:
    client = _RetryableResponsesClient(simple_failures=1)
    adapter = OpenAIAdapter(
        LLMSettings(api_key="test-key", model="gpt-5.4", wire_api="responses"),
        http_client=_SDKRawHTTPClient(client),
    )

    try:
        asyncio.run(adapter.simple_chat([LLMMessage(role="user", content="hello")]))
    except RuntimeError as exc:
        classification = classify_llm_error(exc)
    else:
        raise AssertionError("retryable side-query failure must be surfaced to its caller")

    assert classification.provider_error_type == "busy"
    assert classification.retryable is True
    assert client.responses.calls == 1


def test_openai_adapter_routes_gpt_image_model_to_images_api() -> None:
    client = _ImageResponsesClient()
    adapter = OpenAIAdapter(
        LLMSettings(
            api_key="test-key",
            model="gpt-image-2",
            wire_api="responses",
            max_tokens=4_096,
            reasoning_effort="high",
            reasoning_effort_levels=("high",),
            responses_reasoning_summary="detailed",
            prompt_cache_retention="24h",
        ),
        http_client=_SDKRawHTTPClient(client),
    )

    events = asyncio.run(
        _collect_stream_events(
            adapter.stream_chat(
                [LLMMessage(role="user", content="生成一张夕阳篮球图片")],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "run_command",
                            "description": "Run a command",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
                metadata={
                    "conversation_id": "conv_image",
                    "minicode_session_id": "session_image",
                },
            )
        )
    )

    assert client.responses.calls == 0
    assert client.images.calls == 1
    image_request = client.images.kwargs[0]
    assert image_request["model"] == "gpt-image-2"
    assert image_request["prompt"] == "生成一张夕阳篮球图片"
    assert image_request["n"] == 1
    assert image_request["size"] == "1024x1024"
    assert image_request["response_format"] == "b64_json"
    assert {
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "max_tokens",
        "max_output_tokens",
        "max_completion_tokens",
        "reasoning",
        "reasoning_effort",
        "prompt_cache_retention",
    }.isdisjoint(image_request)

    assert [event.type for event in events] == [
        StreamEventType.TEXT_CHUNK,
        StreamEventType.PROVIDER_ACTIVITY,
        StreamEventType.IMAGE_CHUNK,
        StreamEventType.PROVIDER_ACTIVITY,
        StreamEventType.TEXT_CHUNK,
        StreamEventType.DONE,
    ]
    activities = [
        event.provider_activity
        for event in events
        if event.type == StreamEventType.PROVIDER_ACTIVITY
    ]
    assert [activity.status for activity in activities] == ["running", "completed"]
    assert activities[0].id == activities[1].id
    image = next(event for event in events if event.type == StreamEventType.IMAGE_CHUNK)
    assert image.image_media_type == "image/png"
    assert image.image_data.startswith("iVBORw0KGgo")
    text = "".join(
        event.content for event in events if event.type == StreamEventType.TEXT_CHUNK
    )
    assert "好的，我来生成这张图片" in text
    assert "图像已经为你生成好了" in text
    assert events[-1].finish_reason == "stop"

    try:
        asyncio.run(adapter.simple_chat([LLMMessage(role="user", content="summarize")]))
    except RuntimeError as exc:
        side_classification = classify_llm_error(exc)
    else:
        raise AssertionError("dedicated GPT Image models must not enter side queries")

    assert side_classification.provider_error_type == "unsupported_capability"
    assert client.responses.calls == 0


def test_openai_adapter_keeps_minicode_tool_controls_for_coding_responses_without_tools() -> None:
    client = _RetryableResponsesClient()
    adapter = OpenAIAdapter(
        LLMSettings(api_key="test-key", model="gpt-5.4", wire_api="responses"),
        http_client=_SDKRawHTTPClient(client),
    )

    asyncio.run(
        _collect_stream_events(
            adapter.stream_chat([LLMMessage(role="user", content="hello")])
        )
    )

    request = client.responses.kwargs[0]
    assert request["tools"] == []
    assert request["tool_choice"] == "auto"
    assert request["parallel_tool_calls"] is True


def test_openai_adapter_drops_incomplete_streamed_tool_calls() -> None:
    class _IncompleteToolCallStream:
        async def create(self, **kwargs):
            return _AsyncListStream(
                [
                    SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(
                                    content=None,
                                    tool_calls=[
                                        SimpleNamespace(
                                            index=0,
                                            id="call_blank",
                                            function=SimpleNamespace(name="", arguments='{"query":"北京天气"}'),
                                        ),
                                    ],
                                ),
                                finish_reason=None,
                            )
                        ],
                        usage=None,
                    ),
                    SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(content="请稍后重试。", tool_calls=None),
                                finish_reason=None,
                            )
                        ],
                        usage=None,
                    ),
                    SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(content=None, tool_calls=None),
                                finish_reason="stop",
                            )
                        ],
                        usage=None,
                    ),
                ]
            )

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=_IncompleteToolCallStream()),
        responses=SimpleNamespace(create=None),
    )
    adapter = OpenAIAdapter(
        LLMSettings(api_key="test-key", model="deepseek-v4-flash", wire_api="chat"),
        http_client=_SDKRawHTTPClient(client),
    )

    events = asyncio.run(
        _collect_stream_events(
            adapter.stream_chat(
                [LLMMessage(role="user", content="今天北京天气如何")],
                tools=[{
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "description": "Search web",
                        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
                    },
                }],
            )
        )
    )

    assert [event.type for event in events] == [
        StreamEventType.TEXT_CHUNK,
        StreamEventType.DONE,
    ]
    assert events[0].content == "请稍后重试。"


def test_openai_adapter_refuses_a_partial_streamed_tool_batch() -> None:
    """A tool-calls turn that loses one of two parallel calls must fail closed.

    Dropping the id-less call and executing the survivor runs half of what the
    model asked for. The Responses path already refuses this
    (``terminal_function_call_missing``); Chat must match.
    """

    def _tool_delta(index, call_id, name, arguments):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=index,
                                id=call_id,
                                function=SimpleNamespace(name=name, arguments=arguments),
                            )
                        ],
                    ),
                    finish_reason=None,
                )
            ],
            usage=None,
        )

    class _PartialBatchStream:
        async def create(self, **kwargs):
            return _AsyncListStream(
                [
                    _tool_delta(0, "call_a", "read_file", '{"path":"a.py"}'),
                    # The gateway never sends an id for the second call.
                    _tool_delta(1, None, "run_command", '{"cmd":"ls"}'),
                    SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(content=None, tool_calls=None),
                                finish_reason="tool_calls",
                            )
                        ],
                        usage=None,
                    ),
                ]
            )

    adapter = OpenAIAdapter(
        LLMSettings(api_key="test-key", model="deepseek-v4-flash", wire_api="chat"),
        http_client=_SDKRawHTTPClient(
            SimpleNamespace(
                chat=SimpleNamespace(completions=_PartialBatchStream()),
                responses=SimpleNamespace(create=None),
            )
        ),
    )

    events = asyncio.run(
        _collect_stream_events(
            adapter.stream_chat(
                [LLMMessage(role="user", content="go")],
                tools=[],
            )
        )
    )

    assert not any(event.type == StreamEventType.TOOL_CALL for event in events)
    assert not any(event.type == StreamEventType.DONE for event in events)
    errors = [event for event in events if event.type == StreamEventType.ERROR]
    assert len(errors) == 1
    assert errors[0].raw["protocol_error_code"] == "incomplete_streamed_tool_call"
    assert errors[0].raw["dropped_tool_call_count"] == 1
    assert errors[0].raw["tool_call_count"] == 1


def test_openai_adapter_sdk_stream_aggregates_fragmented_tool_arguments() -> None:
    class _FragmentedToolCallStream:
        async def create(self, **kwargs):
            return _AsyncListStream(
                [
                    SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(
                                    content=None,
                                    tool_calls=[
                                        SimpleNamespace(
                                            index=0,
                                            id="call_pwd",
                                            function=SimpleNamespace(name="run_command", arguments='{"comm'),
                                        )
                                    ],
                                ),
                                finish_reason=None,
                            )
                        ],
                        usage=None,
                    ),
                    SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(
                                    content=None,
                                    tool_calls=[
                                        SimpleNamespace(
                                            index=0,
                                            id="",
                                            function=SimpleNamespace(name="", arguments='and":"pwd"}'),
                                        )
                                    ],
                                ),
                                finish_reason=None,
                            )
                        ],
                        usage=None,
                    ),
                    SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(content=None, tool_calls=None),
                                finish_reason="tool_calls",
                            )
                        ],
                        usage=None,
                    ),
                ]
            )

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=_FragmentedToolCallStream()),
        responses=SimpleNamespace(create=None),
    )
    adapter = OpenAIAdapter(
        LLMSettings(api_key="test-key", model="deepseek-chat", wire_api="chat"),
        http_client=_SDKRawHTTPClient(client),
    )

    events = asyncio.run(
        _collect_stream_events(
            adapter.stream_chat(
                [LLMMessage(role="user", content="where am I")],
                tools=[{
                    "type": "function",
                    "function": {
                        "name": "run_command",
                        "description": "Run command",
                        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
                    },
                }],
            )
        )
    )

    tool_events = [event for event in events if event.type == StreamEventType.TOOL_CALL]
    assert [event.tool_calls_final for event in tool_events] == [False, True]
    assert tool_events[0].tool_calls == [
        ToolCallEvent(id="call_pwd", name="run_command", arguments={"command": "pwd"})
    ]
    assert tool_events[1].tool_calls == [
        ToolCallEvent(id="call_pwd", name="run_command", arguments={"command": "pwd"})
    ]


def test_openai_adapter_sdk_flushes_complete_text_before_tool_boundary() -> None:
    preamble = "我来用 Python 在桌面创建一个 PDF 文件，先检查下可用的库。"

    class _TextThenToolCallStream:
        async def create(self, **kwargs):
            return _AsyncListStream(
                [
                    SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(content=preamble, tool_calls=None),
                                finish_reason=None,
                            )
                        ],
                        usage=None,
                    ),
                    SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(
                                    content=None,
                                    tool_calls=[
                                        SimpleNamespace(
                                            index=0,
                                            id="call_check",
                                            function=SimpleNamespace(name="run_command", arguments='{"command":"python -V"}'),
                                        )
                                    ],
                                ),
                                finish_reason="tool_calls",
                            )
                        ],
                        usage=None,
                    ),
                ]
            )

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=_TextThenToolCallStream()),
        responses=SimpleNamespace(create=None),
    )
    adapter = OpenAIAdapter(
        LLMSettings(api_key="test-key", model="mimo-v2.5-pro", wire_api="chat"),
        http_client=_SDKRawHTTPClient(client),
    )

    events = asyncio.run(
        _collect_stream_events(
            adapter.stream_chat(
                [LLMMessage(role="user", content="创建 PDF")],
                tools=[{
                    "type": "function",
                    "function": {
                        "name": "run_command",
                        "description": "Run command",
                        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
                    },
                }],
            )
        )
    )

    text_events = [event for event in events if event.type == StreamEventType.TEXT_CHUNK]
    tool_boundary = next(
        index for index, event in enumerate(events)
        if event.type in {StreamEventType.TOOL_CALL_START, StreamEventType.TOOL_CALL_DELTA, StreamEventType.TOOL_CALL}
    )
    assert "".join(event.content for event in text_events) == preamble
    assert max(events.index(event) for event in text_events) < tool_boundary


def test_openai_adapter_raw_http_stream_aggregates_fragmented_tool_arguments() -> None:
    lines = [
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_pwd","function":{"name":"run_command","arguments":"{\\"comm"}}]},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"and\\":\\"pwd\\"}"}}]},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]
    adapter = OpenAIAdapter(
        LLMSettings(
            api_key="test-key",
            base_url="https://api.deepseek.com/v1",
            model="deepseek-chat",
            wire_api="chat",
        ),
        http_client=None,
    )
    fake_client = _FakeChatHTTPClient(lines)
    adapter._http_client = fake_client

    events = asyncio.run(
        _collect_stream_events(
            adapter.stream_chat(
                [LLMMessage(role="user", content="where am I")],
                tools=[{
                    "type": "function",
                    "function": {
                        "name": "run_command",
                        "description": "Run command",
                        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
                    },
                }],
            )
        )
    )

    tool_events = [event for event in events if event.type == StreamEventType.TOOL_CALL]
    assert [event.tool_calls_final for event in tool_events] == [False, True]
    assert tool_events[0].tool_calls == [
        ToolCallEvent(id="call_pwd", name="run_command", arguments={"command": "pwd"})
    ]
    assert tool_events[1].tool_calls == [
        ToolCallEvent(id="call_pwd", name="run_command", arguments={"command": "pwd"})
    ]
    assert fake_client.requests[0]["url"] == "https://api.deepseek.com/v1/chat/completions"


def test_openai_adapter_raw_http_flushes_complete_text_before_tool_boundary() -> None:
    preamble = "我来用 Python 在桌面创建一个 PDF 文件，先检查下可用的库。"
    lines = [
        f'data: {{"choices":[{{"delta":{{"content":"{preamble}"}},"finish_reason":null}}]}}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_check","function":{"name":"run_command","arguments":"{\\"command\\":\\"python -V\\"}"}}]},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]
    adapter = OpenAIAdapter(
        LLMSettings(
            api_key="test-key",
            base_url="https://api.deepseek.com/v1",
            model="mimo-v2.5-pro",
            wire_api="chat",
        ),
        http_client=None,
    )
    adapter._http_client = _FakeChatHTTPClient(lines)

    events = asyncio.run(
        _collect_stream_events(
            adapter.stream_chat(
                [LLMMessage(role="user", content="创建 PDF")],
                tools=[{
                    "type": "function",
                    "function": {
                        "name": "run_command",
                        "description": "Run command",
                        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
                    },
                }],
            )
        )
    )

    text_events = [event for event in events if event.type == StreamEventType.TEXT_CHUNK]
    tool_boundary = next(
        index for index, event in enumerate(events)
        if event.type in {StreamEventType.TOOL_CALL_START, StreamEventType.TOOL_CALL_DELTA, StreamEventType.TOOL_CALL}
    )
    assert "".join(event.content for event in text_events) == preamble
    assert max(events.index(event) for event in text_events) < tool_boundary


def test_openai_adapter_raw_http_done_reports_finish_reason_length() -> None:
    lines = [
        'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"length"}]}',
        "data: [DONE]",
    ]
    adapter = OpenAIAdapter(
        LLMSettings(
            api_key="test-key",
            base_url="https://api.deepseek.com/v1",
            model="deepseek-chat",
            wire_api="chat",
        ),
        http_client=None,
    )
    adapter._http_client = _FakeChatHTTPClient(lines)

    events = asyncio.run(
        _collect_stream_events(
            adapter.stream_chat([LLMMessage(role="user", content="long answer")])
        )
    )

    done_event = next(event for event in events if event.type == StreamEventType.DONE)
    assert done_event.finish_reason == "length"


def test_openai_adapter_surfaces_blocked_tool_requests_without_changing_the_request() -> None:
    client = _BlockedToolRetryChatClient()
    adapter = OpenAIAdapter(
        LLMSettings(api_key="test-key", model="gpt-5.4-mini", wire_api="chat"),
        http_client=_SDKRawHTTPClient(client),
    )
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        }
    ]

    events = asyncio.run(
        _collect_stream_events(
            adapter.stream_chat([LLMMessage(role="user", content="hello")], tools=tools)
        )
    )

    assert client.chat.completions.calls == 1
    assert client.chat.completions.seen_tools == [True]
    assert events[-1].type == StreamEventType.ERROR


def test_openai_connection_error_message_is_actionable() -> None:
    message = _clean_error_message(RuntimeError("Connection error."))

    assert message == "Connection error."


def test_run_agent_loop_skips_simple_chat_fallback_for_realtime_search_queries() -> None:
    llm = _RealtimeSearchErrorLLM()
    tool_registry = ToolRegistry()
    tool_registry.register(_FakeWebSearchTool())

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="北京今天天气怎么样",
                llm=llm,
                tool_registry=tool_registry,
                artifact_store=ArtifactStore(),
                permission_checker=PermissionChecker(PermissionSettings()),
                agent_settings=AgentSettings(
                    max_iterations=1,
                    # Disable transient retries for this test so it isolates
                    # the realtime-search no-simple-chat fallback contract.
                    stream_max_attempts=0,
                    stream_retry_delay_seconds=0,
                ),
                token_budget=TokenBudget(),
            )
        )
    )

    error_events = [event for event in events if event.type == "error"]
    assert error_events
    assert error_events[0].data["message"] == "模型服务网络请求失败，请稍后重试。"
    assert error_events[0].data["provider_error_type"] == "network"
    assert llm.simple_chat_calls == 0


def test_run_agent_loop_does_not_prime_realtime_search_before_model_reply() -> None:
    llm = _RealtimeSearchNeedsToolLLM()
    tool_registry = ToolRegistry()
    websearch = _RecordingWebSearchTool()
    tool_registry.register(websearch)

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="北京今天天气怎么样",
                llm=llm,
                tool_registry=tool_registry,
                artifact_store=ArtifactStore(),
                permission_checker=PermissionChecker(PermissionSettings()),
                agent_settings=AgentSettings(max_iterations=1),
                token_budget=TokenBudget(),
            )
        )
    )

    tool_call_events = [event for event in events if event.type == "tool_call"]
    tool_result_events = [event for event in events if event.type == "tool_result"]
    final_events = _final_events(events)

    assert websearch.calls == []
    assert llm.stream_chat_calls == 1
    assert llm.saw_tool_result is False
    assert tool_call_events == []
    assert tool_result_events == []
    assert _final_text(events) == "请告诉我你想让我做什么。"


def test_run_agent_loop_accepts_model_reply_without_weather_text_gate() -> None:
    llm = _ClarifyThenAnswerWeatherLLM()
    state = AgentState(user_message="今天天气如何")
    state.conversation_id = "conv_test_weather_clarify"

    async def run() -> list[AgentEvent]:
        return await _collect_events(
            run_agent_loop(
                "今天天气如何",
                llm,
                ToolRegistry(),
                ArtifactStore(),
                PermissionChecker(PermissionSettings()),
                agent_settings=AgentSettings(max_iterations=3),
                token_budget=TokenBudget(),
                state=state,
            )
        )

    events = asyncio.run(run())
    text = _final_text(events)

    assert text.endswith("北京今天多云，8°C 到 18°C。")
    assert llm.calls == 1


def test_weather_text_does_not_hide_model_tools_or_inject_intent_guidance() -> None:
    class _ClarifyingWeatherLLM(LLMAdapter):
        def __init__(self) -> None:
            self.tool_names: list[str] = []
            self.system_text = ""
            self.message_text = ""

        async def stream_chat(
            self,
            messages: list[LLMMessage],
            tools: list[dict[str, object]] | None = None,
        ) -> AsyncIterator[StreamEvent]:
            self.tool_names = [
                str((tool.get("function") or {}).get("name"))
                for tool in (tools or [])
                if isinstance(tool, dict)
            ]
            self.system_text = "\n".join(msg.content for msg in messages if msg.role == "system")
            self.message_text = "\n".join(msg.content for msg in messages)
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="你想查询哪个城市的天气？")
            yield StreamEvent(type=StreamEventType.DONE)

        async def simple_chat(self, messages: list[LLMMessage]) -> str:
            return "simple fallback should not run"

    llm = _ClarifyingWeatherLLM()
    tool_registry = ToolRegistry()
    tool_registry.register(_FakeWebSearchTool())

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="今天天气如何",
                llm=llm,
                tool_registry=tool_registry,
                artifact_store=ArtifactStore(),
                permission_checker=PermissionChecker(PermissionSettings()),
                agent_settings=AgentSettings(max_iterations=1),
                token_budget=TokenBudget(),
            )
        )
    )

    assert llm.tool_names == []
    assert tool_registry.get_tool_spec("mcp__websearch__search").exposure == "deferred"
    assert "missing a city, area, or physical location" not in llm.message_text
    assert not [event for event in events if event.type == "tool_call"]
    assert _final_text(events) == "你想查询哪个城市的天气？"


def test_question_like_reply_does_not_hide_unrelated_tools() -> None:
    class _ReadFileTool(BaseTool):
        name = "read_file"
        description = "read file"
        permission = PermissionLevel.AUTO

        def get_schema(self) -> object:
            from backend.tools.base import ToolSchema

            return ToolSchema(
                name=self.name,
                description=self.description,
                parameters={"type": "object", "properties": {"file_path": {"type": "string"}}},
            )

        async def execute(self, args: dict[str, object]) -> object:
            return ToolResult(content="should not run")

    class _ClarifyingLLM(LLMAdapter):
        def __init__(self) -> None:
            self.tool_names: list[str] = []

        async def stream_chat(
            self,
            messages: list[LLMMessage],
            tools: list[dict[str, object]] | None = None,
        ) -> AsyncIterator[StreamEvent]:
            self.tool_names = [
                str((tool.get("function") or {}).get("name"))
                for tool in (tools or [])
                if isinstance(tool, dict)
            ]
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="你在哪个城市？")
            yield StreamEvent(type=StreamEventType.DONE)

        async def simple_chat(self, messages: list[LLMMessage]) -> str:
            return "unused"

    llm = _ClarifyingLLM()
    tool_registry = ToolRegistry()
    tool_registry.register(_FakeWebSearchTool())
    tool_registry.register(_ReadFileTool())

    asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="附近有什么好吃的",
                llm=llm,
                tool_registry=tool_registry,
                artifact_store=ArtifactStore(),
                permission_checker=PermissionChecker(PermissionSettings()),
                agent_settings=AgentSettings(max_iterations=1),
                token_budget=TokenBudget(),
            )
        )
    )

    assert set(llm.tool_names) == {"read_file"}
    assert tool_registry.get_tool_spec("mcp__websearch__search").exposure == "deferred"


def test_run_agent_loop_does_not_auto_search_when_model_skips_tool_call() -> None:
    llm = _RealtimeSearchGroundingLLM()
    tool_registry = ToolRegistry()
    tool_registry.register(_FakeWebSearchTool())

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="北京今天天气怎么样",
                llm=llm,
                tool_registry=tool_registry,
                artifact_store=ArtifactStore(),
                permission_checker=PermissionChecker(PermissionSettings()),
                agent_settings=AgentSettings(max_iterations=1),
                token_budget=TokenBudget(),
            )
        )
    )

    text = _final_text(events)

    assert not [event for event in events if event.type == "tool_call"]
    assert not [event for event in events if event.type == "tool_result"]
    assert text


def test_run_agent_loop_keeps_preamble_when_no_tool_result_exists() -> None:
    llm = _RealtimeWeatherPreambleLLM()
    tool_registry = ToolRegistry()
    tool_registry.register(_FakeWebSearchTool())

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="北京今天天气怎么样",
                llm=llm,
                tool_registry=tool_registry,
                artifact_store=ArtifactStore(),
                permission_checker=PermissionChecker(PermissionSettings()),
                agent_settings=AgentSettings(max_iterations=1),
                token_budget=TokenBudget(),
            )
        )
    )

    text = _final_text(events)

    assert "我先查一下" in text
    assert "小雨" not in text


def test_run_agent_loop_does_not_followup_fetch_without_model_tool_call() -> None:
    llm = _RealtimeWeatherPreambleLLM()
    tool_registry = ToolRegistry()
    search_tool = _SearchResultsWebSearchTool()
    fetch_tool = _RecordingFetchPageTool()
    tool_registry.register(search_tool)
    tool_registry.register(fetch_tool)

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="北京今天天气怎么样",
                llm=llm,
                tool_registry=tool_registry,
                artifact_store=ArtifactStore(),
                permission_checker=PermissionChecker(PermissionSettings()),
                agent_settings=AgentSettings(max_iterations=1),
                token_budget=TokenBudget(),
            )
        )
    )

    tool_calls = [event.data["name"] for event in events if event.type == "tool_call"]
    text = _final_text(events)

    assert tool_calls == []
    assert fetch_tool.calls == []
    assert "我先查一下" in text
