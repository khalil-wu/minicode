from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import routes_chat
from backend.conversations.public_projection import project_public_transcript_message, tool_history_revision
import pytest
from backend.conversations.repository import ConversationRepository


def long_message(count):
    return {"id": "answer", "role": "assistant", "content": "Final answer", "terminal_status": "completed",
            "blocks": [{"type": "tool_call", "record": {"id": f"tool-{index}", "name": "read_file",
                        "args": {"file_path": f"f{index}.py"}, "status": "success", "startedAt": index}}
                       for index in range(count)] + [{"type": "text", "itemId": "final", "content": "Final answer",
                                                     "source": "model_final", "status": "completed", "isStreaming": False}]}


def test_long_completed_turn_keeps_answer_and_loads_every_tool_from_indexed_message(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path)
    original = long_message(4200)
    record = repo.create_conversation(transcript=[original], context_snapshot={"history": []})
    assert len(record.transcript[0]["blocks"]) == 4201
    cold = ConversationRepository(tmp_path)
    monkeypatch.setattr(cold, "_read_generation", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("private checkpoint read")))
    page = cold.get_conversation_view(record.id)
    shown = page["transcript"][0]
    assert len(shown["blocks"]) == 41
    assert shown["blocks"][-1]["content"] == "Final answer"
    assert shown["blocks"][-1]["transcriptIndex"] == 4200
    assert shown["tool_page"] == {"before": 4160, "remaining": 4160, "total": 4200, "revision": tool_history_revision(record.transcript[0])}
    assert project_public_transcript_message(shown)["tool_page"] == shown["tool_page"]
    observed = {item["transcriptIndex"] for item in shown["blocks"] if item["type"] == "tool_call"}
    cursor = shown["tool_page"]
    while cursor["remaining"]:
        earlier = cold.get_message_tool_items(record.id, "answer", before=cursor["before"], limit=200, revision=cursor["revision"])
        observed.update(item["transcriptIndex"] for item in earlier["blocks"])
        cursor = earlier["tool_page"]
    assert observed == set(range(4200))
    assert len(ConversationRepository(tmp_path).get_conversation(record.id).transcript[0]["blocks"]) == 4201


def test_active_partial_turn_is_not_windowed_and_terminal_log_projection_is(tmp_path):
    repo = ConversationRepository(tmp_path)
    record = repo.create_conversation(context_snapshot={"history": []})
    active = {**long_message(100), "terminal_status": "partial"}
    repo.commit_turn_projection(record.id, assistant_message=active, context_delta={"set": {}, "removed": []}, partial=True)
    assert "tool_page" not in ConversationRepository(tmp_path).get_conversation_view(record.id)["transcript"][0]
    repo.commit_turn_projection(record.id, assistant_message=long_message(100), context_delta={"set": {}, "removed": []})
    cold = ConversationRepository(tmp_path)
    shown = cold.get_conversation_view(record.id)["transcript"][0]
    assert shown["tool_page"]["remaining"] == 60
    assert len(cold.get_message_tool_items(record.id, "answer", before=60)["blocks"]) == 40


def test_completed_turn_with_unsettled_tool_keeps_that_evidence_visible(tmp_path):
    repo = ConversationRepository(tmp_path)
    message = long_message(90)
    message["blocks"][0]["record"]["status"] = "running"
    record = repo.create_conversation(transcript=[message])
    shown = ConversationRepository(tmp_path).get_conversation_view(record.id)["transcript"][0]
    assert "tool_page" not in shown
    assert shown["blocks"][0]["record"]["status"] == "running"


def test_tool_items_rest_endpoint_uses_session_and_cursor_contract(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path)
    record = repo.create_conversation(transcript=[long_message(90)])
    monkeypatch.setattr(routes_chat._state, "ws_manager", SimpleNamespace(
        get_session=lambda sid: SimpleNamespace(conversation_repo=repo) if sid == "session" else None))
    app = FastAPI()
    app.include_router(routes_chat.router)
    with TestClient(app) as client:
        url = f"/api/conversations/{record.id}/messages/answer/tools"
        response = client.get(url, params={"session_id": "session", "before": 50})
        assert response.status_code == 200
        assert response.json()["tool_page"] == {"before": 10, "remaining": 10, "total": 90, "revision": tool_history_revision(record.transcript[0])}
        assert client.get(url, params={"session_id": "missing", "before": 50}).status_code == 404
        assert client.get(url, params={"session_id": "session", "before": 999}).status_code == 409


def test_tool_cursor_survives_unrelated_turns_but_rejects_replaced_message(tmp_path):
    repo = ConversationRepository(tmp_path)
    record = repo.create_conversation(transcript=[long_message(90)])
    page = repo.get_conversation_view(record.id)["transcript"][0]["tool_page"]
    repo.append_transcript_message(record.id, {"id": "next", "role": "user", "content": "next task"})
    assert repo.get_message_tool_items(record.id, "answer", before=page["before"], revision=page["revision"])
    changed = long_message(90)
    changed["blocks"][0]["record"]["args"] = {"file_path": "different.py"}
    repo.upsert_transcript_message(record.id, changed)
    with pytest.raises(ValueError, match="message changed"):
        repo.get_message_tool_items(record.id, "answer", before=page["before"], revision=page["revision"])
