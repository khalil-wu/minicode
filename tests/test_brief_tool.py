import pytest

from backend.services.tool_registry_factory import build_tool_registry as _build_tool_registry
from backend.artifact.store import ArtifactStore
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.agent_tools import BriefTool
from backend.tools.base import PermissionLevel


@pytest.mark.asyncio
async def test_brief_tool_emits_agent_message_item() -> None:
    events: list[tuple[str, dict[str, object]]] = []

    async def emit(event_type: str, data: dict[str, object]) -> None:
        events.append((event_type, data))

    tool = BriefTool()
    result = await tool.execute(
        {"message": "Done. I updated the tests.", "status": "normal"},
        context=ToolExecutionContext(
            permission=PermissionContext(),
            session_id="sess-1",
            emit_event=emit,
        ),
    )

    assert result.is_error is False
    assert result.result_kind == "reply"
    assert events == [
        ("item.started", {
            "item": {
                "id": "agent-message",
                "type": "agent_message",
                "text": "",
                "status": "in_progress",
            },
        }),
        ("item.completed", {
            "item": {
                "id": "agent-message",
                "type": "agent_message",
                "text": "Done. I updated the tests.",
                "source": "reply",
                "status": "completed",
            },
        }),
    ]


@pytest.mark.asyncio
async def test_brief_tool_rejects_empty_message() -> None:
    tool = BriefTool()
    result = await tool.execute({"message": "  "}, context=ToolExecutionContext(permission=PermissionContext()))

    assert result.is_error is True
    assert "Missing message argument" in result.content


def test_brief_tool_permission_and_spec() -> None:
    tool = BriefTool()
    spec = tool.get_spec()

    assert tool.permission is PermissionLevel.AUTO
    assert tool.mutates_workspace is False
    assert spec is not None
    assert spec.capability == "user.reply"
    assert spec.exposure == "deferred"


def test_brief_tool_is_registered() -> None:
    registry = _build_tool_registry(ArtifactStore())

    assert registry.get_tool("send_message") is not None


@pytest.mark.asyncio
async def test_brief_tool_includes_attachments_in_completed_item(tmp_path) -> None:
    image = tmp_path / "shot.png"
    image_bytes = b"\x89PNG\r\n\x1a\n"
    image.write_bytes(image_bytes)
    log = tmp_path / "build.log"
    log_bytes = b"ok\n"
    log.write_bytes(log_bytes)

    events: list[tuple[str, dict[str, object]]] = []

    async def emit(event_type: str, data: dict[str, object]) -> None:
        events.append((event_type, data))

    tool = BriefTool()
    result = await tool.execute(
        {"message": "See the screenshot and log.", "attachments": [str(image), str(log)]},
        context=ToolExecutionContext(
            permission=PermissionContext(),
            session_id="sess-1",
            workspace_root=tmp_path,
            emit_event=emit,
        ),
    )

    assert result.is_error is False
    # Summary reflects the attachment count.
    assert "2 attachments included" in result.content
    completed = events[-1]
    assert completed[0] == "item.completed"
    attachments = completed[1]["attachments"]
    assert isinstance(attachments, list)
    assert len(attachments) == 2
    by_path = {item["path"]: item for item in attachments}
    assert by_path[str(image)]["is_image"] is True
    assert by_path[str(log)]["is_image"] is False
    assert by_path[str(image)]["size"] == len(image_bytes)
    assert by_path[str(log)]["size"] == len(log_bytes)


@pytest.mark.asyncio
async def test_brief_tool_resolves_relative_attachments_against_workspace_root(tmp_path) -> None:
    rel = tmp_path / "report.txt"
    rel.write_text("hi", encoding="utf-8")

    events: list[tuple[str, dict[str, object]]] = []

    async def emit(event_type: str, data: dict[str, object]) -> None:
        events.append((event_type, data))

    tool = BriefTool()
    await tool.execute(
        {"message": "Report attached.", "attachments": ["report.txt"]},
        context=ToolExecutionContext(
            permission=PermissionContext(),
            session_id="sess-1",
            workspace_root=tmp_path,
            emit_event=emit,
        ),
    )

    chunk = events[-1][1]
    attachments = chunk["attachments"]
    assert len(attachments) == 1
    assert attachments[0]["path"] == str(rel)


@pytest.mark.asyncio
async def test_brief_tool_skips_attachments_outside_workspace_or_denylist(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.log"
    outside.write_text("secret", encoding="utf-8")
    denied = workspace / ".env"
    denied.write_text("TOKEN=secret", encoding="utf-8")
    allowed = workspace / "report.log"
    allowed.write_text("ok", encoding="utf-8")

    events: list[tuple[str, dict[str, object]]] = []

    async def emit(event_type: str, data: dict[str, object]) -> None:
        events.append((event_type, data))

    tool = BriefTool()
    await tool.execute(
        {"message": "Report attached.", "attachments": [str(outside), str(denied), "report.log"]},
        context=ToolExecutionContext(
            permission=PermissionContext(),
            session_id="sess-1",
            workspace_root=workspace,
            emit_event=emit,
        ),
    )

    chunk = events[-1][1]
    attachments = chunk["attachments"]
    assert len(attachments) == 1
    assert attachments[0]["path"] == str(allowed)


@pytest.mark.asyncio
async def test_brief_tool_skips_missing_attachments_gracefully(tmp_path) -> None:
    events: list[tuple[str, dict[str, object]]] = []

    async def emit(event_type: str, data: dict[str, object]) -> None:
        events.append((event_type, data))

    tool = BriefTool()
    result = await tool.execute(
        {"message": "Done.", "attachments": [str(tmp_path / "does-not-exist.png")]},
        context=ToolExecutionContext(
            permission=PermissionContext(),
            session_id="sess-1",
            workspace_root=tmp_path,
            emit_event=emit,
        ),
    )

    # All attachments missing → no attachments key on the payload, no error.
    assert result.is_error is False
    chunk = events[-1][1]
    assert "attachments" not in chunk


@pytest.mark.asyncio
async def test_brief_tool_without_attachments_has_unchanged_payload(tmp_path) -> None:
    events: list[tuple[str, dict[str, object]]] = []

    async def emit(event_type: str, data: dict[str, object]) -> None:
        events.append((event_type, data))

    tool = BriefTool()
    await tool.execute(
        {"message": "Plain reply."},
        context=ToolExecutionContext(
            permission=PermissionContext(),
            session_id="sess-1",
            workspace_root=tmp_path,
            emit_event=emit,
        ),
    )

    chunk = events[-1][1]
    assert "attachments" not in chunk
