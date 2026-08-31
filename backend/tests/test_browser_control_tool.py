from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pytest

from backend.tools.base import PermissionLevel
from backend.tools.browser_control_tool import BrowserControlTool
from backend.permissions.context import PermissionContext, ToolExecutionContext


_ONE_PIXEL_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
_MINIMAL_JPEG_B64 = "/9j/4AAQSkZJRg=="
_MINIMAL_WEBP_B64 = "UklGRgAAAABXRUJQ"


def test_browser_control_rejects_remote_cdp_endpoint() -> None:
    tool = BrowserControlTool()

    message = tool.validate_input({"action": "list_targets", "cdp_endpoint": "http://10.0.0.2:9222"})

    assert "must be localhost" in message


def test_browser_control_navigation_requires_confirmation() -> None:
    tool = BrowserControlTool()

    level = tool.check_permission({"action": "navigate", "url": "https://example.com"})

    assert level == PermissionLevel.CONFIRM
    assert tool.is_read_only({"action": "navigate"}) is False
    assert tool.is_read_only({"action": "list_targets"}) is True


def test_browser_control_interactive_actions_require_confirmation() -> None:
    tool = BrowserControlTool()

    for action in {"navigate", "click", "type", "press_key", "scroll", "evaluate"}:
        assert tool.check_permission({"action": action}) == PermissionLevel.CONFIRM
        assert tool.is_read_only({"action": action}) is False

    for action in {"get_text", "get_html", "get_dom", "wait_for_element", "get_console_logs", "get_network_logs", "screenshot"}:
        assert tool.check_permission({"action": action, "selector": "body"}) == PermissionLevel.CONFIRM
        assert tool.is_read_only({"action": action}) is True

    for action in {"discover", "list_targets", "get_url"}:
        assert tool.check_permission({"action": action}) == PermissionLevel.AUTO
        assert tool.is_read_only({"action": action}) is True


def test_browser_control_schema_exposes_interactive_actions() -> None:
    tool = BrowserControlTool()
    action_schema = tool.get_schema().parameters["properties"]["action"]

    assert "click" in action_schema["enum"]
    assert "type" in action_schema["enum"]
    assert "get_dom" in action_schema["enum"]
    assert "wait_for_element" in action_schema["enum"]
    assert "get_network_logs" in action_schema["enum"]


def test_browser_control_validates_interactive_args() -> None:
    tool = BrowserControlTool()

    assert "selector or x/y" in tool.validate_input({"action": "click"})
    assert "Missing text" in tool.validate_input({"action": "type"})
    assert "Missing key" in tool.validate_input({"action": "press_key"})
    assert "Missing expression" in tool.validate_input({"action": "evaluate"})
    assert "Missing selector" in tool.validate_input({"action": "wait_for_element"})


def test_embedded_browser_accepts_workspace_html_and_rejects_unusable_scheme(monkeypatch) -> None:
    monkeypatch.setenv("MINICODE_EMBEDDED_BROWSER_ENDPOINT", "http://127.0.0.1:43123")
    tool = BrowserControlTool()

    # A workspace HTML target is a valid navigation request; execute serves it
    # over loopback rather than rejecting it at validation.
    assert tool.validate_input({"action": "navigate", "url": "file:///C:/workspace/index.html"}) == ""

    message = tool.validate_input({"action": "navigate", "url": "ftp://example.com/x"})
    assert "http or https" in message


def test_browser_control_lists_targets(monkeypatch) -> None:
    calls: list[str] = []

    class _Response:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

        def raise_for_status(self) -> None:
            return None

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str):
            calls.append(url)
            return _Response([
                {
                    "id": "page-1",
                    "type": "page",
                    "title": "MiniCode",
                    "url": "http://127.0.0.1:5173/",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/page-1",
                }
            ])

    monkeypatch.setattr("backend.tools.browser_control_tool.httpx.AsyncClient", _Client)

    result = asyncio.run(BrowserControlTool().execute({"action": "list_targets"}))

    assert not result.is_error
    assert "page-1 [page] MiniCode" in result.content
    assert calls == ["http://127.0.0.1:9222/json/list"]


def test_browser_control_prefers_authenticated_embedded_browser_bridge(monkeypatch) -> None:
    calls: list[tuple[str, dict, dict]] = []

    class _Response:
        status_code = 200

        def json(self):
            return {
                "ok": True,
                "targets": [{
                    "id": "browser_tab_1",
                    "type": "page",
                    "title": "MiniCode Browser",
                    "url": "https://example.com/",
                }],
            }

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, *, headers: dict, json: dict):
            calls.append((url, headers, json))
            return _Response()

    monkeypatch.setenv("MINICODE_EMBEDDED_BROWSER_ENDPOINT", "http://127.0.0.1:43123")
    monkeypatch.setenv("MINICODE_EMBEDDED_BROWSER_TOKEN", "bridge-token")
    monkeypatch.setattr("backend.tools.browser_control_tool.httpx.AsyncClient", _Client)

    result = asyncio.run(BrowserControlTool().execute(
        {"action": "list_targets"},
        ToolExecutionContext(
            permission=PermissionContext(),
            conversation_id="conv-browser-owner",
        ),
    ))

    assert not result.is_error
    assert "browser_tab_1 [page] MiniCode Browser" in result.content
    assert calls == [(
        "http://127.0.0.1:43123/v1/command",
        {"authorization": "Bearer bridge-token"},
        {"action": "list_targets", "conversation_id": "conv-browser-owner"},
    )]


def test_browser_control_get_url_uses_page_target(monkeypatch) -> None:
    class _Response:
        def json(self):
            return [
                {
                    "id": "page-1",
                    "type": "page",
                    "title": "MiniCode",
                    "url": "http://127.0.0.1:5173/",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/page-1",
                }
            ]

        def raise_for_status(self) -> None:
            return None

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str):
            return _Response()

    monkeypatch.setattr("backend.tools.browser_control_tool.httpx.AsyncClient", _Client)

    result = asyncio.run(BrowserControlTool().execute({"action": "get_url"}))

    assert not result.is_error
    assert "Title: MiniCode" in result.content
    assert "URL: http://127.0.0.1:5173/" in result.content


def test_browser_control_get_text_uses_runtime_evaluate(monkeypatch) -> None:
    calls: list[tuple[str, dict | None]] = []

    async def _select_target(self, endpoint: str, target_id: str):
        return {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/page-1"}

    class _Session:
        async def call(self, method: str, params: dict | None = None):
            calls.append((method, params))
            if method == "Runtime.evaluate":
                return {"result": {"value": "hello browser"}}
            return {}

    class _SessionContext:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    monkeypatch.setattr(BrowserControlTool, "_select_target", _select_target)
    monkeypatch.setattr("backend.tools.browser_control_tool._cdp_session", lambda ws: _SessionContext())

    result = asyncio.run(BrowserControlTool().execute({"action": "get_text"}))

    assert not result.is_error
    assert result.content == "hello browser"
    assert calls[0][0] == "Runtime.enable"
    assert calls[1][0] == "Runtime.evaluate"


def test_embedded_screenshot_persists_owner_scoped_typed_image_artifact(monkeypatch) -> None:
    captured: dict = {}

    class _Response:
        status_code = 200

        def json(self):
            return {
                "ok": True,
                "data": _ONE_PIXEL_PNG_B64,
                "mimeType": "image/png",
                "target": {"id": "tab-1", "title": "Preview", "url": "http://localhost/"},
            }

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, *, headers: dict, json: dict):
            return _Response()

    class _Store:
        def save(self, content, source, type="text", preview_lines=5, conversation_id=None, workspace_root=None, media_type=""):
            captured.update(locals())
            return "art_screen"

    monkeypatch.setenv("MINICODE_EMBEDDED_BROWSER_ENDPOINT", "http://127.0.0.1:43123")
    monkeypatch.setenv("MINICODE_EMBEDDED_BROWSER_TOKEN", "bridge-token")
    monkeypatch.setattr("backend.tools.browser_control_tool.httpx.AsyncClient", _Client)

    result = asyncio.run(BrowserControlTool().execute(
        {"action": "screenshot"},
        ToolExecutionContext(
            permission=PermissionContext(),
            conversation_id="conv-screen",
            workspace_root=Path("C:/workspace"),
            artifact_store=_Store(),
        ),
    ))

    assert not result.is_error
    assert result.artifact_id == "art_screen"
    assert result.artifact_kind == "image"
    assert result.artifact_media_type == "image/png"
    assert result.artifact_bytes == 68
    assert captured["content"] == _ONE_PIXEL_PNG_B64
    assert captured["type"] == "image"
    assert captured["media_type"] == "image/png"
    assert captured["conversation_id"] == "conv-screen"
    assert str(captured["workspace_root"]) == "C:\\workspace"


def test_cdp_screenshot_persists_pure_base64_image_artifact(monkeypatch) -> None:
    captured: dict = {}

    async def _select_target(self, endpoint: str, target_id: str):
        return {"id": "page-1", "title": "CDP", "url": "http://localhost/", "webSocketDebuggerUrl": "ws://localhost/devtools/page/1"}

    class _Session:
        async def call(self, method: str, params: dict | None = None):
            if method == "Page.captureScreenshot":
                return {"data": _ONE_PIXEL_PNG_B64}
            return {}

    class _SessionContext:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    class _Store:
        def save(self, content, source, type="text", preview_lines=5, conversation_id=None, workspace_root=None, media_type=""):
            captured.update(locals())
            return "art_cdp"

    monkeypatch.setattr(BrowserControlTool, "_select_target", _select_target)
    monkeypatch.setattr("backend.tools.browser_control_tool._cdp_session", lambda ws: _SessionContext())
    result = asyncio.run(BrowserControlTool().execute(
        {"action": "screenshot"},
        ToolExecutionContext(
            permission=PermissionContext(),
            conversation_id="conv-cdp",
            workspace_root=Path("C:/workspace"),
            artifact_store=_Store(),
        ),
    ))

    assert not result.is_error
    assert result.artifact_id == "art_cdp"
    assert captured["content"] == _ONE_PIXEL_PNG_B64
    assert captured["type"] == "image"
    assert captured["media_type"] == "image/png"
    assert captured["conversation_id"] == "conv-cdp"


@pytest.mark.parametrize(
    ("media_type", "data", "byte_count"),
    [
        ("image/jpeg", _MINIMAL_JPEG_B64, 10),
        ("image/webp", _MINIMAL_WEBP_B64, 12),
    ],
)
def test_embedded_screenshot_reports_the_actual_image_mime_type(
    monkeypatch,
    media_type: str,
    data: str,
    byte_count: int,
) -> None:
    class _Response:
        status_code = 200

        def json(self):
            return {
                "ok": True,
                "data": data,
                "mimeType": media_type,
                "target": {"id": "tab-1", "title": "Preview", "url": "http://localhost/"},
            }

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, *, headers: dict, json: dict):
            return _Response()

    class _Store:
        def save(self, content, source, type="text", preview_lines=5, conversation_id=None, workspace_root=None, media_type=""):
            return "art-mime"

    monkeypatch.setenv("MINICODE_EMBEDDED_BROWSER_ENDPOINT", "http://127.0.0.1:43123")
    monkeypatch.setenv("MINICODE_EMBEDDED_BROWSER_TOKEN", "bridge-token")
    monkeypatch.setattr("backend.tools.browser_control_tool.httpx.AsyncClient", _Client)

    result = asyncio.run(BrowserControlTool().execute(
        {"action": "screenshot"},
        ToolExecutionContext(
            permission=PermissionContext(),
            conversation_id="conv-screen",
            workspace_root=Path("C:/workspace"),
            artifact_store=_Store(),
        ),
    ))

    assert not result.is_error
    assert f"{media_type} bytes: {byte_count}" in result.content
    assert "PNG bytes:" not in result.content
