from __future__ import annotations

import asyncio

import pytest

from backend.artifact.store import ArtifactStore
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.services.tool_registry_factory import build_tool_registry
from backend.tools.sleep_tool import SleepTool
from backend.tools.toolsets import ToolsetPolicy


def test_sleep_waits_briefly() -> None:
    result = asyncio.run(SleepTool().execute({"duration_ms": 1}))

    assert result.is_error is False
    assert "elapsed_seconds" in result.content
    assert result.result_kind == "status"


def test_sleep_is_cancel_aware() -> None:
    cancel_event = asyncio.Event()
    cancel_event.set()
    context = ToolExecutionContext(
        permission=PermissionContext(),
        cancel_event=cancel_event,
    )

    result = asyncio.run(SleepTool().execute({"duration_ms": 5000}, context=context))

    assert "elapsed_seconds" in result.content
    assert result.duration_ms < 1000


@pytest.mark.parametrize("supports_search_tool", [False, True])
def test_sleep_exposure_follows_selected_model_search_capability(tmp_path, supports_search_tool) -> None:
    registry = build_tool_registry(ArtifactStore(storage_dir=tmp_path / "artifacts"))

    assert registry.get_tool("sleep") is not None
    policy = ToolsetPolicy(supports_search_tool=supports_search_tool)
    names = {schema["function"]["name"] for schema in registry.get_schemas(toolset_policy=policy)}
    assert "sleep" in names
    view = registry.get_schema_view("sleep", toolset_policy=policy)
    assert view.exposure == "direct_model_only" and view.direct
