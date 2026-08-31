from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from backend.tools.browser_control_tool import BrowserControlTool


def _target(index: int, *, target_type: str = "page") -> dict[str, str]:
    return {
        "id": f"target-{index}",
        "type": target_type,
        "title": f"Target {index}",
        "url": f"https://example.com/{index}",
        "webSocketDebuggerUrl": f"ws://127.0.0.1:9222/devtools/page/target-{index}",
    }


def test_browser_target_display_limit_does_not_limit_exact_target_lookup() -> None:
    tool = BrowserControlTool()
    targets = [_target(index) for index in range(35)]
    tool._fetch_all_targets = AsyncMock(return_value=targets)

    displayed = asyncio.run(tool._list_targets("http://127.0.0.1:9222"))
    selected = asyncio.run(tool._select_target("http://127.0.0.1:9222", "target-34"))

    assert len(displayed) == tool._MAX_TARGETS
    assert displayed[-1]["id"] == "target-29"
    assert selected["id"] == "target-34"


def test_browser_exact_target_lookup_validates_hidden_target_type() -> None:
    tool = BrowserControlTool()
    targets = [_target(index) for index in range(30)] + [_target(30, target_type="worker")]
    tool._fetch_all_targets = AsyncMock(return_value=targets)

    try:
        asyncio.run(tool._select_target("http://127.0.0.1:9222", "target-30"))
    except RuntimeError as exc:
        assert str(exc) == "Target is not a page: target-30"
    else:
        raise AssertionError("non-page exact targets must not be selectable")
