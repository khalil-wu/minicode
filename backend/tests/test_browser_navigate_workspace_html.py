"""Navigating to a workspace HTML file serves it instead of failing."""

from __future__ import annotations

import asyncio
from pathlib import Path

from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools import browser_control_tool as bct
import backend.tools.browser_support as browser_support
from backend.tools.browser_control_tool import BrowserControlTool


def _context(workspace: Path) -> ToolExecutionContext:
    return ToolExecutionContext(
        permission=PermissionContext(mode="bypass"),
        workspace_root=workspace,
        session_id="session-nav",
        conversation_id="conv-nav",
    )


def test_workspace_html_file_is_accepted_and_served(tmp_path, monkeypatch) -> None:
    # The model writes an HTML file and asks to open it. Rejecting the request
    # and naming preview_server cost a full extra turn every time, so a
    # workspace HTML target is now served over the owned loopback preview.
    page = tmp_path / "鹈鹕骑车.html"
    page.write_text("<!DOCTYPE html><title>bike</title>", encoding="utf-8")
    served: list[Path] = []

    async def fake_serve(target, context):
        served.append(Path(target))
        return "http://127.0.0.1:59023/token/page.html", ""

    monkeypatch.setattr(browser_support, "_serve_workspace_file_for_navigation", fake_serve)

    tool = BrowserControlTool()
    # validate_input must not reject the spelling before execute can resolve it.
    assert tool.validate_input({"action": "navigate", "url": page.as_uri()}) == ""
    assert tool.validate_input({"action": "navigate", "url": "鹈鹕骑车.html"}) == ""

    resolved, error = asyncio.run(
        bct._resolved_navigation_url(page.as_uri(), _context(tmp_path)),
    )
    assert error == ""
    assert resolved == "http://127.0.0.1:59023/token/page.html"
    assert served == [page.resolve()]

    relative, relative_error = asyncio.run(
        bct._resolved_navigation_url("鹈鹕骑车.html", _context(tmp_path)),
    )
    assert relative_error == ""
    assert relative == "http://127.0.0.1:59023/token/page.html"


def test_http_urls_and_unusable_schemes_keep_their_existing_behaviour(tmp_path) -> None:
    tool = BrowserControlTool()

    # An http URL passes through untouched — no preview server is involved.
    assert tool.validate_input({"action": "navigate", "url": "https://example.com"}) == ""
    passthrough, error = asyncio.run(
        bct._resolved_navigation_url("https://example.com/page", _context(tmp_path)),
    )
    assert (passthrough, error) == ("https://example.com/page", "")

    # A scheme that can never be a local HTML file is still refused up front.
    assert "http or https" in tool.validate_input({
        "action": "navigate",
        "url": "ftp://example.com/x.html",
    })
    assert "http or https" in tool.validate_input({
        "action": "navigate",
        "url": "file:///etc/passwd",
    })

    # A path outside the workspace is refused with a concrete reason.
    outside = tmp_path.parent / "elsewhere.html"
    outside.write_text("<html></html>", encoding="utf-8")
    _url, escape_error = asyncio.run(
        bct._resolved_navigation_url(outside.as_uri(), _context(tmp_path)),
    )
    assert "not an existing HTML file inside the" in escape_error


def test_missing_workspace_file_is_reported_rather_than_served(tmp_path) -> None:
    _url, error = asyncio.run(
        bct._resolved_navigation_url("does-not-exist.html", _context(tmp_path)),
    )
    assert "not an existing HTML file inside the" in error
