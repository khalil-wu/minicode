"""Committed directory pages, cold DB reads and the actual desktop projection."""
from __future__ import annotations

from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading
import json

from fastapi import FastAPI
import httpx
import pytest

from backend.api import routes_chat
from backend.conversations.repository import ConversationRepository
from backend.conversations.sidebar_order import project_key
from backend.tests.test_user_input_presentation_chain import session_for


@pytest.fixture
def directory_case(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.workspace.recent_projects.DEFAULT_STORE_PATH", tmp_path / "projects.json")
    repo = ConversationRepository(tmp_path / "conversations")
    records = [repo.create_conversation(conversation_id=f"conv_dir_{index:04}", title=f"Task {index:02}",
        workspace_root=str(tmp_path / ("a" if index % 2 else "b"))) for index in range(32)]
    return repo, records


def ids(page):
    return [item["id"] for item in page["data"]]


def test_forward_backward_exclusions_and_stable_cursor_after_deletion(directory_case):
    repo, records = directory_case
    expected = [item.id for item in reversed(records)]
    first = repo.directory.page({"limit": 7})
    assert ids(first) == expected[:7]
    second = repo.directory.page({"limit": 7, "cursor": first["next_cursor"], "use_state_db_only": True})
    assert ids(second) == expected[7:14]
    backwards = repo.directory.page({"limit": 7, "cursor": second["backwards_cursor"], "use_state_db_only": True})
    assert ids(backwards) == expected[:7]
    repo.delete_conversation(first["data"][-1]["id"])
    after_delete = repo.directory.page({"limit": 7, "cursor": first["next_cursor"], "use_state_db_only": True})
    assert ids(after_delete) == expected[7:14]
    excluded = repo.directory.page({"limit": 7, "excluded_thread_ids": expected[:4], "use_state_db_only": True})
    assert ids(excluded) == [identifier for identifier in expected if identifier not in {*expected[:4], expected[6]}][:7]
    assert len(repo.directory.page({"limit": 0})["data"]) == 1
    assert len(repo.directory.page({"limit": 999})["data"]) == 31


def test_filtering_sections_projects_provider_source_and_relations(directory_case, tmp_path):
    repo, records = directory_case
    section = repo.thread_sections.create("Work")
    repo.thread_sections.move_thread(records[0].id, section["id"])
    repo.thread_sections.move_thread(records[1].id, section["id"])
    repo.set_conversation_project(records[0].id, "named-project")
    repo.update_model_selection(records[0].id, provider="vertex", model="fixture")
    child = repo.create_conversation(title="Child", parent_conversation_id=records[0].id, thread_source="subagent")
    grandchild = repo.create_conversation(title="Grandchild", parent_conversation_id=child.id, thread_source="subagent")
    repo.set_archived(records[2].id, True)
    repo.directory.page()
    page = lambda **query: repo.directory.page({"use_state_db_only": True, **query})
    assert ids(page(section_id=section["id"], sort_key="section_position")) == [records[1].id, records[0].id]
    assert records[1].id not in ids(page(section_id=None, limit=100))
    assert ids(page(project_id="named-project")) == [records[0].id]
    assert records[0].id not in ids(page(project_id=None, limit=100))
    assert ids(page(model_providers=["vertex"])) == [records[0].id]
    assert set(ids(page(source_kinds=["subagent"]))) == {child.id, grandchild.id}
    assert child.id not in ids(page(limit=100))
    assert ids(page(parent_thread_id=records[0].id)) == [child.id]
    assert set(ids(page(ancestor_thread_id=records[0].id))) == {child.id, grandchild.id}
    assert ids(page(parent_thread_id=records[0].id, source_kinds=["subagent"])) == [child.id]
    assert set(ids(page(ancestor_thread_id=records[0].id, source_kinds=["subagent"]))) == {child.id, grandchild.id}
    assert ids(page(archived=True)) == [records[2].id]
    assert ids(page(search_term="Task 03")) == [records[3].id]
    assert all(item["workspace_root"] == str(tmp_path / "a") for item in page(cwd=str(tmp_path / "a"))["data"])


@pytest.mark.parametrize("query", [{"cursor": "not-valid"}, {"originators": ["unsupported"]},
    {"parent_thread_id": "one", "ancestor_thread_id": "two"}, {"project_id": ""},
    {"sort_key": "invalid"}, {"excluded_thread_ids": ["conv_invalid"] * 101}])
def test_invalid_directory_queries_report_errors(directory_case, query):
    repo, _ = directory_case
    with pytest.raises(ValueError):
        repo.directory.page(query)


def test_db_only_never_scans_history_and_observes_other_repository_commits(directory_case, monkeypatch):
    repo, records = directory_case
    with pytest.raises(ValueError, match="not initialized"):
        repo.directory.page({"use_state_db_only": True})
    repo.directory.page()
    writer = ConversationRepository(repo.storage_root)
    writer.rename_conversation(records[0].id, "Updated by another writer")
    writer.set_archived(records[1].id, True)
    writer.delete_conversation(records[2].id)
    reader = ConversationRepository(repo.storage_root)
    monkeypatch.setattr(reader, "_ensure_summary_index_loaded", lambda: pytest.fail("DB-only pages must not scan manifests"))
    monkeypatch.setattr(reader, "_load_record", lambda *_: pytest.fail("Directory pages must not read model history"))
    page = reader.directory.page({"limit": 100, "use_state_db_only": True})
    assert next(item for item in page["data"] if item["id"] == records[0].id)["title"] == "Updated by another writer"
    assert records[1].id not in ids(page) and records[2].id not in ids(page)
    assert page["inventory_revision"] == repo.list_conversations_with_revision()[1]


def test_failed_derived_publication_is_repaired_from_the_committed_manifest(directory_case, monkeypatch):
    repo, records = directory_case
    repo.directory.page()
    commit = repo.directory.committed
    def unavailable(*_args, **_kwargs):
        raise RuntimeError("injected directory write failure")
    monkeypatch.setattr(repo.directory, "committed", unavailable)
    with pytest.raises(RuntimeError, match="directory write failure"):
        repo.rename_conversation(records[0].id, "Durably renamed")
    monkeypatch.setattr(repo.directory, "committed", commit)
    repo.directory.ensure_current()
    page = repo.directory.page({"search_term": "Durably renamed", "use_state_db_only": True})
    assert ids(page) == [records[0].id]


def test_unchanged_directory_reads_preserve_index_without_reencoding_summaries(directory_case, monkeypatch):
    repo, records = directory_case
    first = repo.directory.page({"limit": 7})
    connect = repo.directory.connect
    writes = []

    @contextmanager
    def observe_writes():
        with connect() as connection:
            before = connection.total_changes
            yield connection
            writes.append(connection.total_changes - before)

    monkeypatch.setattr(repo.directory, "connect", observe_writes)
    monkeypatch.setattr(repo.directory, "_values", lambda *_: pytest.fail("Unchanged summaries must not be reencoded"))
    assert repo.directory.page({"limit": 7}) == first
    second = repo.directory.page({"limit": 7, "cursor": first["next_cursor"]})
    assert ids(first) + ids(second) == [record.id for record in reversed(records[-14:])]
    assert [item.id for item in repo.list_conversations()] == [record.id for record in reversed(records)]
    assert writes and not any(writes)


def test_reused_directory_observes_external_files_even_after_a_direct_summary_read(tmp_path):
    repo = ConversationRepository(tmp_path / "external")
    path = repo.storage_root / "conv_external.json"
    payload = {"id": "conv_external", "title": "Initial", "transcript": []}
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert repo.directory.page()["data"][0]["title"] == "Initial"
    path.write_text(json.dumps({**payload, "title": "Externally replaced title"}), encoding="utf-8")
    assert repo.get_conversation_summary(payload["id"]).title == "Externally replaced title"
    assert repo.directory.page()["data"][0]["title"] == "Externally replaced title"
    added = repo.storage_root / "conv_added_item.json"
    added.write_text(json.dumps({**payload, "id": "conv_added_item"}), encoding="utf-8")
    assert set(ids(repo.directory.page())) == {"conv_external", "conv_added_item"}
    path.write_text("{broken", encoding="utf-8")
    assert ids(repo.directory.page()) == ["conv_added_item"]
    added.unlink()
    assert repo.directory.page()["data"] == []


def test_reused_directory_rebuilds_when_derived_publication_is_missing(directory_case):
    repo, records = directory_case
    first = repo.directory.page({"limit": 100})
    with repo.directory.connect() as connection:
        connection.execute("DELETE FROM directory_state")
        connection.execute("DELETE FROM threads")
    rebuilt = repo.directory.page({"limit": 100})
    assert rebuilt == first
    assert len(rebuilt["data"]) == len(records)


@pytest.mark.parametrize("warm", [False, True])
def test_directory_scan_releases_publication_lock_and_retries_concurrent_commit(directory_case, monkeypatch, warm):
    repo, records = directory_case
    if warm:
        repo.directory.page()
    writer = ConversationRepository(repo.storage_root)
    entered, release = threading.Event(), threading.Event()
    scan = repo._ensure_summary_index_loaded
    calls = []
    def hold_scan():
        scan()
        calls.append(True)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
    monkeypatch.setattr(repo, "_ensure_summary_index_loaded", hold_scan)
    with ThreadPoolExecutor(max_workers=2) as workers:
        reading = workers.submit(repo.directory.page, {"limit": 100})
        assert entered.wait(5)
        try:
            workers.submit(writer.rename_conversation, records[0].id, "Concurrent rename").result(timeout=3)
        finally:
            release.set()
        page = reading.result(timeout=5)
    assert len(calls) == 2
    assert next(item for item in page["data"] if item["id"] == records[0].id)["title"] == "Concurrent rename"


def test_legacy_summary_preserves_metadata_without_hydrating_or_normalizing_history(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path / "legacy")
    payload = {"id": "conv_legacy_summary", "title": "Old conversation", "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z", "transcript": [{"id": "u", "role": "user", "content": "original"},
            {"id": "a", "role": "assistant", "content": "answer"}],
        "context_snapshot": {"history": [{"role": "user", "runtime_context": "<environment_context><cwd>C:/original</cwd></environment_context>"}]},
        "model_selection": {"provider": "custom", "model": "original"}, "pinned": True,
        "memory_pollution_sources": ["untrusted-output"], "resource_references": [{"id": "resource", "payload": {"value": "original"}}]}
    path = repo.storage_root / (payload["id"] + ".json")
    path.write_text(json.dumps(payload), encoding="utf-8")
    original_bytes = path.read_bytes()
    monkeypatch.setattr(repo, "_load_record", lambda *_: pytest.fail("A summary must not hydrate model history"))
    monkeypatch.setattr("backend.conversations.repository._normalize_loaded_transcript", lambda *_: pytest.fail("A summary must not normalize transcript blocks"))
    summary = repo.directory.page()["data"][0]
    assert summary["message_count"] == 2
    assert summary["execution_cwd"] == "C:/original"
    assert summary["memory_polluted"] and summary["pinned"]
    assert summary["model_selection"] == payload["model_selection"]
    assert summary["resource_references"] == payload["resource_references"]
    assert "context_snapshot" not in summary and "transcript" not in summary
    assert path.read_bytes() == original_bytes


def test_summary_scan_uses_discovered_layout_but_direct_reads_observe_later_file_changes(tmp_path):
    repo = ConversationRepository(tmp_path / "legacy")
    identifier = "conv_summary_changed"
    path = repo.storage_root / (identifier + ".json")
    path.write_text(json.dumps({"id": identifier, "title": "First", "transcript": []}), encoding="utf-8")
    assert repo.directory.page()["data"][0]["title"] == "First"
    path.write_text(json.dumps({"id": identifier, "title": "Changed on disk", "transcript": [1, 2, 3]}), encoding="utf-8")
    summary = repo.get_conversation_summary(identifier)
    assert summary.title == "Changed on disk" and summary.message_count == 3
    path.write_text("{broken", encoding="utf-8")
    assert repo.directory.page()["data"] == []
    from backend.conversations.repository import ConversationStorageCorruptError
    with pytest.raises(ConversationStorageCorruptError):
        repo.get_conversation_summary(identifier)


def test_bulk_legacy_scan_keeps_generation_cache_reads_on_the_lock_owner(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path / "mixed")
    original = repo.create_conversation(title="Generation metadata")
    manifest_path = repo.storage_root / f"{original.id}.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("metadata")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    for index in range(256):
        identifier = f"conv_parallel_{index:04}"
        (repo.storage_root / (identifier + ".json")).write_text(json.dumps({"id": identifier, "title": str(index), "transcript": []}), encoding="utf-8")
    reader = ConversationRepository(repo.storage_root)
    owning_thread = threading.get_ident()
    read = reader._read_generation_metadata
    calls = []
    def observed(*args):
        assert threading.get_ident() == owning_thread
        calls.append(args)
        return read(*args)
    monkeypatch.setattr(reader, "_read_generation_metadata", observed)
    summaries = reader.list_conversations()
    assert len(summaries) == 257 and len(calls) == 1
    assert next(item for item in summaries if item.id == original.id).title == "Generation metadata"


def test_sidebar_refresh_is_bounded_and_publishes_known_changes_and_deletions(directory_case):
    repo, records = directory_case
    repo.set_conversation_pinned(records[0].id, True)
    first = repo.directory.inventory(active_id=records[1].id)
    known = {item["id"] for item in first["conversations"]}
    assert len(known) <= 27 and first["directory_page"]["next_cursor"]
    assert records[0].id in known and records[1].id in known
    repo.rename_conversation(records[1].id, "Known old conversation")
    repo.delete_conversation(records[0].id)
    second = repo.directory.inventory(known_ids=known, since_revision=first["inventory_revision"])
    assert second["partial_inventory"]
    assert second["removed_conversation_ids"] == [records[0].id]
    assert next(item for item in second["conversations"] if item["id"] == records[1].id)["title"] == "Known old conversation"
    assert all("context_snapshot" not in item and "transcript" not in item for item in second["conversations"])


def test_project_heads_and_manual_pages_keep_saved_order(directory_case, tmp_path):
    repo, records = directory_case
    selected = [item.id for item in reversed(records) if item.workspace_root == str(tmp_path / "a")]
    repo.set_sidebar_sort_mode("manual", {})
    state = repo._read_sidebar_order()
    state["recent_thread_ids"] = [item.id for item in records]
    state["project_thread_orders"][project_key(str(tmp_path / "a"))] = list(reversed(selected))
    repo._write_sidebar_order(state)
    repo.directory.ensure_current()
    first = repo.directory.sidebar_page(limit=8)
    second = repo.directory.sidebar_page(limit=8, cursor=first["next_cursor"])
    assert ids(first) + ids(second) == [item.id for item in records[:16]]
    inventory = repo.directory.inventory(project_keys=[project_key(str(tmp_path / "a"))])
    assert inventory["directory_page"]["project_cursors"][project_key(str(tmp_path / "a"))]
    assert set(reversed(selected[-5:])).issubset({item["id"] for item in inventory["conversations"]})


def test_named_project_pages_include_legacy_source_folders_without_reassigning_explicit_membership(directory_case, tmp_path):
    from backend.workspace.recent_projects import RecentProjectStore

    repo, records = directory_case
    (tmp_path / "a").mkdir()
    project = RecentProjectStore().create("Named project", [str(tmp_path / "a")])
    repo.set_conversation_project(records[1].id, "")
    repo.directory.ensure_current()
    page = repo.directory.sidebar_page(project_key=project_key(project.path), limit=100)
    expected = {item.id for item in records if item.workspace_root == str(tmp_path / "a")} - {records[1].id}
    assert set(ids(page)) == expected
    assert all(item["project_id"] is None for item in page["data"])


@pytest.mark.asyncio
async def test_http_pages_bind_the_original_session_and_preserve_null_filters(directory_case, monkeypatch):
    repo, records = directory_case
    repo.set_conversation_project(records[0].id, "explicit-project")
    session = SimpleNamespace(conversation_repo=repo, _directory_known_ids=set())
    monkeypatch.setattr(routes_chat._state, "ws_manager", SimpleNamespace(get_session=lambda name: session if name == "owner" else None))
    app = FastAPI()
    app.include_router(routes_chat.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://directory") as client:
        first = await client.post("/api/conversations/list?session_id=owner", json={"limit": 100})
        assert first.status_code == 200
        unassigned = await client.post("/api/conversations/list?session_id=owner", json={"limit": 100, "project_id": None, "use_state_db_only": True})
        assert records[0].id in ids(first.json()) and records[0].id not in ids(unassigned.json())
        assert (await client.post("/api/conversations/list?session_id=foreign", json={})).status_code == 404
        assert (await client.post("/api/conversations/list?session_id=owner", json={"cursor": "broken"})).status_code == 422
        assert (await client.post("/api/conversations/list?session_id=owner", json={"unknown_filter": True})).status_code == 422
    assert session._directory_known_ids


@pytest.mark.asyncio
async def test_real_ws_inventory_keeps_active_owner_and_sends_only_a_page(tmp_path, monkeypatch):
    session, active, _ = session_for(tmp_path, monkeypatch)
    monkeypatch.setattr("backend.workspace.recent_projects.DEFAULT_STORE_PATH", tmp_path / "projects.json")
    for index in range(40):
        session.conversation_repo.create_conversation(title=f"Other {index}")
    try:
        await session.send_conversation_list()
        event = next(item for item in session.ws.messages if item.get("type") == "conversation.list")
        assert event["partial_inventory"] and len(event["conversations"]) == 26
        assert event["active_conversation_id"] == active.id
        assert active.id in {item["id"] for item in event["conversations"]}
        assert event["directory_page"]["next_cursor"]
    finally:
        await session.session_lifecycle.shutdown(reason="directory-test")
