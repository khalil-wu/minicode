from __future__ import annotations

import asyncio

from backend.artifact.store import ArtifactStore
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.services.tool_registry_factory import build_tool_registry
from backend.tools.sleep_tool import SleepTool


def test_sleep_waits_briefly() -> None:
    result = asyncio.run(SleepTool().execute({"seconds": 0}))

    assert result.is_error is False
    assert "Waited" in result.content
    assert result.result_kind == "status"


def test_sleep_is_cancel_aware() -> None:
    cancel_event = asyncio.Event()
    cancel_event.set()
    context = ToolExecutionContext(
        permission=PermissionContext(),
        cancel_event=cancel_event,
    )

    result = asyncio.run(SleepTool().execute({"seconds": 5}, context=context))

    assert result.is_error is True
    assert result.status == "cancelled"
    assert "cancelled" in result.content


def test_sleep_is_registered_as_deferred_tool(tmp_path) -> None:
    registry = build_tool_registry(ArtifactStore(storage_dir=tmp_path / "artifacts"))

    assert registry.get_tool("sleep") is not None
    names = {schema["function"]["name"] for schema in registry.get_schemas()}
    assert "sleep" not in names
