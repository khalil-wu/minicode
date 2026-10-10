from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import routes_chat
from backend.conversations.public_projection import project_public_transcript_message, turn_diff_file_summaries
from backend.conversations.repository import ConversationRepository
from backend.ws.payload_contracts import validate_session_projection_payload
from backend.ws.session_restore import SessionRestoreManager


def _message(conversation_id, patch, *, source="tool", truncated=False):
    return {"id": "answer", "role": "assistant", "content": "Page complete", "terminal_status": "failed",
            "termination_reason": "api_error", "metadata": {"turn_diff": {
                "thread_id": conversation_id, "conversation_id": conversation_id, "message_id": "answer",
                "turn_id": "turn-1", "revision": 3, "diff": patch, "source": source,
                **({"truncated": truncated} if truncated is not None else {}),
            }}}


def _large_patch():
    return "diff --git a/index.html b/index.html\n--- a/index.html\n+++ b/index.html\n@@ -0,0 +5000 @@\n" + ("+" + "x" * 1024 + "\n") * 5000


def test_wire_page_defers_large_diff_but_persistence_and_indexed_read_retain_every_character(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path)
    patch = _large_patch()
    record = repo.create_conversation(conversation_id="conv-diff-history", transcript=[_message("conv-diff-history", patch)])
    assert record.transcript[0]["metadata"]["turn_diff"]["diff"] == patch
    cold = ConversationRepository(tmp_path)
    monkeypatch.setattr(cold, "_read_generation", lambda *args: (_ for _ in ()).throw(AssertionError("private checkpoint read")))
    view = cold.get_conversation_view(record.id)
    shown = view["transcript"][0]["metadata"]["turn_diff"]
    assert shown["diff"] is None and shown["deferred"] is True and shown["truncated"] is False
    assert shown["files"] == [{"path": "index.html", "additions": 5000, "deletions": 0}]
    validate_session_projection_payload({"type": "conversation.switched", "conversation_id": record.id, "conversation": view})
    result = cold.get_message_turn_diff(record.id, "answer", turn_id="turn-1", revision=3)
    assert result["diff"] == patch and result["truncated"] is False
    assert project_public_transcript_message(view["transcript"][0])["metadata"]["turn_diff"] == shown


@pytest.mark.asyncio
async def test_legacy_workspace_diff_restores_without_rewriting_or_hiding_the_old_record(tmp_path):
    repo = ConversationRepository(tmp_path)
    header = "diff --git a/qa/chrome-profile/listdata.json b/qa/chrome-profile/listdata.json\n--- /dev/null\n+++ b/qa/chrome-profile/listdata.json\n@@ -0,0 +1 @@\n+"
    patch = header + "x" * (4_194_304 - len(header) - 3) + "..."
    record = repo.create_conversation(conversation_id="conv-legacy-diff", transcript=[_message("conv-legacy-diff", patch, source="workspace_snapshot", truncated=None)])
    before = record.transcript[0]["metadata"]["turn_diff"]["diff"]
    restored = await SessionRestoreManager(ConversationRepository(tmp_path)).restore_session("session-diff", record.id)
    assert restored["restored"] and restored["error"] is None
    metadata = restored["messages"][0]["metadata"]["turn_diff"]
    assert metadata["diff"] is None and metadata["deferred"] and metadata["truncated"]
    validate_session_projection_payload({"type": "conversation.switched", "conversation_id": record.id,
                                         "conversation": ConversationRepository(tmp_path).get_conversation_view(record.id)})
    after = ConversationRepository(tmp_path).get_message_turn_diff(record.id, "answer", turn_id="turn-1", revision=3)
    assert after["diff"] == before and after["source"] == "workspace_snapshot" and after["truncated"]


def test_deferred_http_read_uses_exact_message_turn_revision_and_rejects_retired_owners(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path)
    patch = _large_patch()
    record = repo.create_conversation(conversation_id="conv-http-diff", transcript=[_message("conv-http-diff", patch)])
    session = SimpleNamespace(conversation_repo=ConversationRepository(tmp_path))
    monkeypatch.setattr(routes_chat._state, "ws_manager", SimpleNamespace(get_session=lambda owner: session if owner == "session-diff" else None))
    app = FastAPI()
    app.include_router(routes_chat.router)
    client = TestClient(app)
    url = f"/api/conversations/{record.id}/messages/answer/turn-diff"
    params = {"session_id": "session-diff", "turn_id": "turn-1", "revision": 3}
    response = client.get(url, params=params)
    assert response.status_code == 200 and response.json()["diff"] == patch
    assert client.get(url, params={**params, "revision": 2}).status_code == 409
    assert client.get(url, params={**params, "turn_id": "other-turn"}).status_code == 409
    assert client.get(url, params={**params, "session_id": "other-session"}).status_code == 404
    assert client.get(url.replace("/answer/", "/other-message/"), params=params).status_code == 404
    assert repo.delete_conversation(record.id)
    assert client.get(url, params=params).status_code == 404


def test_diff_summary_keeps_rename_binary_and_literal_header_lines_inside_hunks():
    patch = ("diff --git a/old name.js b/new name.js\nsimilarity index 90%\nrename from old name.js\nrename to new name.js\n"
             "--- a/old name.js\n+++ b/new name.js\n@@ -1 +1,2 @@\n-old\n+++ b/literal-content\n+new\n"
             "diff --git a/icon.png b/icon.png\nBinary files a/icon.png and b/icon.png differ\n")
    assert turn_diff_file_summaries(patch) == [
        {"path": "new name.js", "old_path": "old name.js", "additions": 2, "deletions": 1},
        {"path": "icon.png", "additions": 0, "deletions": 0, "is_binary": True},
    ]


@pytest.mark.parametrize("invalid_file", [None, 4, {"path": "a.js", "additions": -1, "deletions": 0},
                                          {"path": "a.js", "additions": 1, "deletions": 0, "is_binary": "yes"}])
def test_restore_contract_rejects_invalid_deferred_file_summaries(invalid_file):
    message = _message("conv-diff-contract", None)
    message["metadata"]["turn_diff"].update(deferred=True, files=[invalid_file])
    with pytest.raises(ValueError):
        validate_session_projection_payload({"type": "conversation.switched", "conversation_id": "conv-diff-contract",
                                             "conversation": {"id": "conv-diff-contract", "transcript": [message]}})
