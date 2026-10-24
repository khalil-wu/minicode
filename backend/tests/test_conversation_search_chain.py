from __future__ import annotations

from contextlib import contextmanager
import re
import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.api import _state
from backend.api.routes_chat import router
from backend.conversations.context_delta import context_snapshot_delta
from backend.conversations.models import ConversationSummary
from backend.conversations.repository import ConversationRepository
from backend.conversations import transcript_index
from backend.services.conversation_search_service import conversation_search_page
from backend.services.chat_api_service import ChatApiServiceError


def session(repository, *, enabled=True):
    return SimpleNamespace(conversation_repo=repository,
                           config=SimpleNamespace(feature_flags=SimpleNamespace(enabled=lambda name: enabled)))


def conversation(repository, title, texts, *, archived=False, root="", created="2026-10-01", kind="main"):
    record = repository.create_conversation(title=title, memory_mode="disabled", workspace_root=root,
                                            conversation_type=kind, transcript=[
        {"id": f"m{index}", "role": "user" if index % 2 == 0 else "assistant", "content": text}
        for index, text in enumerate(texts)])
    record.archived = archived
    record.created_at = created
    repository.save_conversation(record)
    return record


def test_api_finds_body_before_recent_page_without_private_provider_or_tool_matches(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path)
    hit = conversation(repo, "No title match", ["older body-only NEEDLE", *[f"later {i}" for i in range(240)]])
    hidden = conversation(repo, "Private fields", ["visible request", "visible reply"])
    hidden.context_snapshot = {"history": [{"role": "developer", "content": "private-only"}]}
    hidden.transcript += [{"id": "system", "role": "system", "content": "system-only"},
                          {"id": "tools", "role": "assistant", "content": "", "blocks": [
                              {"type": "thinking", "content": "thinking-only"},
                              {"type": "text", "content": "runtime-only", "source": "runtime"},
                              {"type": "tool_call", "record": {"id": "tool", "name": "exec", "args": {"command": "tool-only"}}}]}]
    repo.save_conversation(hidden)
    archive = conversation(repo, "Archived", ["needle archived"], archived=True)
    conversation(repo, "Side chat", ["needle side chat"], kind="side_chat")
    other_repo = ConversationRepository(tmp_path / "other")
    other = conversation(other_repo, "Other owner", ["needle other store"])
    owners = {"local": session(repo), "other": session(other_repo), "disabled": session(repo, enabled=False)}
    monkeypatch.setattr(_state.ws_manager, "get_session", owners.get)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        page = client.get("/api/conversations/search", params={"session_id": "local", "query": " needle "}).json()
        assert [(r["conversation"]["id"], r["message_id"]) for r in page["data"]] == [(hit.id, "m0")]
        assert "NEEDLE" in page["data"][0]["snippet"]
        assert "context_snapshot" not in page["data"][0]["conversation"]
        assert set(page["data"][0]["conversation"]) == {
            "id", "title", "created_at", "updated_at", "conversation_type", "archived", "project_id",
            "workspace_root", "execution_cwd", "worktree_path", "git_isolated"}
        for query in ["private-only", "system-only", "thinking-only", "tool-only", "runtime-only"]:
            assert client.get("/api/conversations/search", params={"session_id": "local", "query": query}).json()["data"] == []
        assert client.get("/api/conversations/search", params={"session_id": "local", "query": "needle", "archived": True}).json()["data"][0]["conversation"]["id"] == archive.id
        assert client.get("/api/conversations/search", params={"session_id": "other", "query": "needle"}).json()["data"][0]["conversation"]["id"] == other.id
        assert client.get("/api/conversations/search", params={"session_id": "disabled", "query": "needle"}).status_code == 403
        assert client.get("/api/conversations/search", params={"session_id": "missing", "query": "needle"}).status_code == 404
        assert client.get("/api/conversations/search", params={"session_id": "local", "query": " "}).status_code == 422


def test_search_pagination_keeps_project_archive_sort_and_session_owner(tmp_path):
    repo = ConversationRepository(tmp_path)
    a = conversation(repo, "A", ["needle"], archived=True, root=str(tmp_path / "alpha"), created="2026-10-02")
    b = conversation(repo, "B", ["needle"], archived=True, root=str(tmp_path / "alpha"), created="2026-10-01")
    conversation(repo, "C", ["needle"], archived=True, root=str(tmp_path / "beta"))
    owner = session(repo)
    params = dict(session_id="owner", query="needle", archived=True, project=str(tmp_path / "alpha"), sort="created", limit=1)
    first = conversation_search_page(owner, **params)
    second = conversation_search_page(owner, **params, cursor=first["next_cursor"])
    assert [r["conversation"]["id"] for r in first["data"] + second["data"]] == [a.id, b.id]
    assert second["next_cursor"] is None
    for changed in [{"session_id": "other"}, {"query": "different"}, {"archived": False}, {"sort": "updated"}]:
        with pytest.raises(ChatApiServiceError, match="different session or query"):
            conversation_search_page(owner, **{**params, **changed}, cursor=first["next_cursor"])


@pytest.mark.parametrize("position", [0, 997])
def test_cold_compressed_long_transcript_uses_one_forward_stream_and_stops_at_first_match(tmp_path, monkeypatch, position):
    repo = ConversationRepository(tmp_path)
    texts = ["ordinary text " * 30 for _ in range(1000)]
    texts[position] = "needle in this message"
    record = conversation(repo, "Long history", texts)
    manifest = repo._read_manifest(record.id)
    path = repo._generation_paths(record.id, manifest["current_generation"])[1]
    assert repo.compress_history_file(path, cutoff_ns=time.time_ns() + 1_000_000_000)
    cold = ConversationRepository(tmp_path)
    monkeypatch.setattr(cold, "_load_record", lambda *_: pytest.fail("search must use the public index, not inflate the provider checkpoint"))
    opens, reads = [], []
    original_open = transcript_index.open_history_binary
    original_read = transcript_index._read_indexed_message

    @contextmanager
    def opened(target):
        opens.append(target)
        with original_open(target) as stream:
            yield stream

    def read(stream, row):
        reads.append(row[0])
        return original_read(stream, row)

    monkeypatch.setattr(transcript_index, "open_history_binary", opened)
    monkeypatch.setattr(transcript_index, "_read_indexed_message", read)
    result = cold.find_transcript_match(record.id, re.compile("needle", re.I))
    assert result["message_id"] == f"m{position}"
    assert opens == [path]
    assert len(reads) == position + 1


def test_search_uses_current_partial_replacement_and_user_display_text(tmp_path):
    repo = ConversationRepository(tmp_path)
    record = repo.create_conversation(title="Projection", memory_mode="disabled", transcript=[
        {"id": "question", "role": "user", "content": "hidden-provider-prefix", "display_content": "visible question"},
        {"id": "answer", "role": "assistant", "content": "old answer"}])
    base = repo.get_projection_context(record.id)
    repo.commit_turn_projection(record.id, assistant_message={"id": "answer", "role": "assistant", "content": "latest needle",
        "blocks": [{"type": "text", "content": "latest needle", "source": "model_final"}]},
        context_delta=context_snapshot_delta(base[1], {"history": []}), expected_revision=base[0], partial=True, return_record=False)
    cold = ConversationRepository(tmp_path)
    assert cold.find_transcript_match(record.id, re.compile("needle"))["message_id"] == "answer"
    assert cold.find_transcript_match(record.id, re.compile("hidden-provider-prefix")) is None
    assert cold.find_transcript_match(record.id, re.compile("visible question"))["message_id"] == "question"


def test_non_matching_scan_is_bounded_and_cursor_advances_before_later_match():
    summaries = [ConversationSummary(id=f"conv_{i:06}", title=str(i), created_at="2026-10-01", updated_at=f"{999-i:04}") for i in range(120)]
    scanned = []

    def find(cid, _):
        scanned.append(cid)
        return {"message_id": "old", "snippet": "needle"} if cid == summaries[103].id else None

    repo = SimpleNamespace(list_conversations_with_revision=lambda **_: ("store", 1, summaries), find_transcript_match=find)
    owner = session(repo)
    first = conversation_search_page(owner, session_id="owner", query="needle", limit=1)
    assert first["data"] == [] and first["next_cursor"]
    assert len(scanned) == 50
    second = conversation_search_page(owner, session_id="owner", query="needle", limit=1, cursor=first["next_cursor"])
    assert second["data"] == [] and len(scanned) == 100
    third = conversation_search_page(owner, session_id="owner", query="needle", limit=1, cursor=second["next_cursor"])
    assert third["data"][0]["conversation"]["id"] == summaries[103].id
    assert len(scanned) == 104
