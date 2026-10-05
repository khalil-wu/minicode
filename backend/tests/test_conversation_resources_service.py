from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.artifact.store import ArtifactStore
from backend.attachments.store import AttachmentStore
from backend.services.chat_api_service import ChatApiServiceError
from backend.services.conversation_resources_service import background_command_detail, conversation_resource_page
from backend.terminal.manager import BackgroundCommand, BackgroundCommandManager
from backend.terminal.task_output import get_task_output_path
from backend.terminal.task_persistence import save_task


def test_resources_cold_inventory_pages_searches_and_keeps_composite_owners(tmp_path):
    root = str(tmp_path / "workspace")
    artifacts = ArtifactStore(storage_dir=tmp_path / "artifacts")
    artifact_id = artifacts.save("old output", source="tool_exec", conversation_id="owner", workspace_root=root, media_type="text/plain")
    artifacts.save("private", source="foreign", conversation_id="other", workspace_root=root)
    attachments = AttachmentStore(tmp_path / "attachments")
    attachments.save(artifact_id=artifact_id, content="upload", metadata={"conversation_id": "owner", "workspace_root": root,
        "attachment": {"file_name": "old-image.png", "media_type": "image/png", "size_bytes": 6}})
    attachments.save(artifact_id="different-workspace", content="private", metadata={"conversation_id": "owner", "workspace_root": str(tmp_path / "other"),
        "attachment": {"file_name": "private.txt"}})
    transcript = [{"id": "old-producer", "turn_id": "old-turn", "role": "assistant", "timestamp": 1000,
        "artifacts": [{"artifactId": artifact_id, "summary": "historical-report.txt"}],
        "tool_calls": [{"id": "old-call", "name": "tool_exec", "artifact_id": artifact_id, "artifact_kind": "text", "artifact_media_type": "text/plain"}]}]
    transcript += [{"id": f"later-{index}", "role": "assistant", "content": "later", "timestamp": 1001 + index} for index in range(100)]
    conversation = SimpleNamespace(id="owner", archived=False, workspace_root=root, transcript=transcript)
    session = SimpleNamespace(conversation_repo=SimpleNamespace(get_conversation=lambda identifier: conversation if identifier == "owner" else None),
        session_lifecycle=SimpleNamespace(workspace_root_for_conversation=lambda record: record.workspace_root),
        artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"), attachment_store=AttachmentStore(tmp_path / "attachments"))
    manager = SimpleNamespace(get_session=lambda identifier: session if identifier == "session" else None)
    first = conversation_resource_page(session_id="session", conversation_id="owner", ws_manager=manager, limit=1, expected_workspace_root=root)
    second = conversation_resource_page(session_id="session", conversation_id="owner", ws_manager=manager, limit=1, after=first["after"], expected_workspace_root=root)
    assert first["total"] == 2 and first["has_more"] and not second["has_more"]
    assert {first["items"][0]["source"], second["items"][0]["source"]} == {"artifact", "attachment"}
    searched = conversation_resource_page(session_id="session", conversation_id="owner", ws_manager=manager, query="historical-report", kind="execution")
    assert searched["items"][0]["message_id"] == "old-producer"
    assert searched["items"][0]["turn_id"] == "old-turn"
    images = conversation_resource_page(session_id="session", conversation_id="owner", ws_manager=manager, kind="image")
    assert [item["name"] for item in images["items"]] == ["old-image.png"]
    with pytest.raises(ChatApiServiceError, match="workspace changed"):
        conversation_resource_page(session_id="session", conversation_id="owner", ws_manager=manager, expected_workspace_root=str(tmp_path / "other"))


def test_background_detail_reads_owned_log_cursor_and_terminal_state(tmp_path):
    output = tmp_path / "command.log"
    output.write_text("first line\nsecond line\n", encoding="utf-8")
    manager = BackgroundCommandManager(session_id="session")
    manager._commands["selected"] = BackgroundCommand(command_id="selected", command="owned command", cwd=str(tmp_path),
        conversation_id="owner", output_path=str(output), status="completed", started_at=10, completed_at=12, exit_code=0)
    session = SimpleNamespace(session_id="session", background_manager=manager,
        conversation_repo=SimpleNamespace(get_conversation_summary=lambda identifier: SimpleNamespace(archived=False)))
    first = background_command_detail(session, "owner", "selected", cursor=0, max_chars=6)
    second = background_command_detail(session, "owner", "selected", cursor=first["next_cursor"], max_chars=100)
    assert first["has_more"] and first["output"] + second["output"] == output.read_bytes().decode("utf-8")
    assert second["status"] == "completed" and second["exit_code"] == 0 and not second["has_more"]
    with pytest.raises(ChatApiServiceError, match="not found"):
        background_command_detail(session, "other", "selected", cursor=0, max_chars=100)


def test_background_history_reads_durable_log_without_claiming_a_live_process(tmp_path, monkeypatch):
    monkeypatch.setenv("MINICODE_STATE_ROOT", str(tmp_path))
    save_task("session", "prior", "old command", "old run", str(tmp_path), None, 10, 0, status="interrupted", conversation_id="owner", cleanup_completed_at=12)
    path = get_task_output_path("session", "owner", "prior")
    path.write_text("retained output", encoding="utf-8")
    session = SimpleNamespace(session_id="session", background_manager=BackgroundCommandManager(session_id="session"),
        conversation_repo=SimpleNamespace(get_conversation_summary=lambda identifier: SimpleNamespace(archived=False)))
    detail = background_command_detail(session, "owner", "prior", cursor=0, max_chars=100)
    assert detail["output"] == "retained output" and detail["status"] == "unknown" and detail["managed"] is False


@pytest.mark.asyncio
async def test_stop_endpoint_calls_only_selected_manager_command_with_owner(tmp_path, monkeypatch):
    from backend.api import _state, routes_chat
    from backend.ws.command_scope import CommandScope

    output = tmp_path / "stop.log"
    output.write_text("before stop", encoding="utf-8")
    manager = BackgroundCommandManager(session_id="session")
    command = BackgroundCommand(command_id="selected", command="owned command", conversation_id="owner", output_path=str(output), started_at=10)
    manager._commands[command.command_id] = command
    async def cancel(identifier, *, conversation_id):
        command.status = "cancelled"
        command.completed_at = 12
        command.exit_code = -1
        return True
    manager.cancel = AsyncMock(side_effect=cancel)
    session = SimpleNamespace(background_manager=manager, conversation_repo=SimpleNamespace(get_conversation_summary=lambda identifier: SimpleNamespace(archived=False)))
    monkeypatch.setattr(_state, "ws_manager", SimpleNamespace(get_session=lambda identifier: session))
    monkeypatch.setattr(routes_chat, "resolve_command_scope", lambda session, data: CommandScope(data["conversation_id"], "", ""))
    result = await routes_chat.stop_background_command("owner", "selected", "session")
    manager.cancel.assert_awaited_once_with("selected", conversation_id="owner")
    assert result["stopped"] and result["status"] == "cancelled" and result["output"] == "before stop"
