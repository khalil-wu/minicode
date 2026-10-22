from __future__ import annotations

from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from backend.api import routes_thread_metadata as routes
from backend.conversations.repository import ConversationRepository
from backend.conversations.thread_resources import pull_request_identity, pull_request_resource, resource_reference
from backend.services import thread_pull_request_service as pr_service
from backend.tools.thread_resource_tools import ThreadResourceTool
from backend.workspace.recent_projects import RecentProjectStore
from backend.atomic_io import canonical_file_path_key


@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.workspace.recent_projects.DEFAULT_STORE_PATH", tmp_path / "recent_projects.json")
    original = ConversationRepository(tmp_path / "original")
    foreign = ConversationRepository(tmp_path / "foreign")
    sessions = {
        "original-session": SimpleNamespace(conversation_repo=original, is_connected=True, send_conversation_list=AsyncMock(), send_payload=AsyncMock(),
            environment_manager=None, active_conversation_id="unused"),
        "same-store-window": SimpleNamespace(conversation_repo=ConversationRepository(original.storage_root),
            is_connected=True, send_conversation_list=AsyncMock(), send_payload=AsyncMock()),
        "other-store-window": SimpleNamespace(conversation_repo=foreign, is_connected=True, send_conversation_list=AsyncMock(), send_payload=AsyncMock()),
    }
    monkeypatch.setattr(routes._state, "ws_manager", SimpleNamespace(get_session=sessions.get, iter_sessions=lambda: sessions.values()))
    app = FastAPI()
    app.include_router(routes.router)
    return original, sessions, app


def test_sections_keep_archived_slots_pins_clone_and_delete_on_cold_read(stores):
    repository, _sessions, _app = stores
    sections = repository.thread_sections
    first = sections.create("  Source section  ", appearance={"icon": "folder", "color": "blue"})
    second = sections.create("Second", after_section_id=first["id"])
    assert UUID(first["id"]).version == 7
    assert first["name"] == "Source section"
    original = repository.create_conversation(sidebar_section_id=first["id"], context_snapshot={"history": [{"role": "user", "content": "saved"}]})
    hidden = repository.create_conversation(sidebar_section_id=first["id"])
    last = repository.create_conversation(sidebar_section_id=first["id"])
    sections.reorder_items(first["id"], "thread:" + original.id, "thread:" + last.id,
        ["thread:" + last.id, "thread:" + original.id])
    assert sections.list()[0]["item_keys"] == ["thread:" + original.id, "thread:" + hidden.id, "thread:" + last.id]
    repository.set_archived(hidden.id, True)
    project_path = str(repository.storage_root / "project")
    RecentProjectStore().add(project_path, "Project")
    sections.move_project(project_path, first["id"], before_item_key="thread:" + hidden.id)
    clone = repository.clone_conversation(original.id)
    cold = ConversationRepository(repository.storage_root)
    catalog = cold.thread_sections.list()
    assert catalog[0]["item_keys"][:2] == ["thread:" + original.id, "thread:" + clone.id]
    assert "thread:" + hidden.id in catalog[0]["item_keys"]
    cold.thread_sections.move_thread(original.id, "pinned")
    assert cold.get_conversation_summary(original.id).pinned
    assert all("thread:" + original.id not in section["item_keys"] for section in cold.thread_sections.list())
    cold.thread_sections.move_thread(original.id, second["id"])
    assert not cold.get_conversation_summary(original.id).pinned
    cold.thread_sections.delete(first["id"])
    assert cold.get_conversation(clone.id) is not None
    assert cold.get_conversation_summary(hidden.id).archived
    cold.delete_conversation(original.id)
    assert cold.thread_sections.list()[0]["item_keys"] == []


def test_resource_membership_is_independent_of_history_and_scoped_keyset_cursors(stores):
    repository, _sessions, _app = stores
    first, second = repository.create_conversation(), repository.create_conversation()
    identity, payload = pull_request_resource("https://GitHub.Example./OWNER/Repo/pull/42/files?diff=1#discussion")
    added = repository.thread_resources.add(first.id, "pull_request", identity, payload)
    revision = repository.get_conversation_summary(first.id).revision
    duplicate = repository.thread_resources.add(first.id, "pull_request", identity, {"url": "ignored"})
    assert duplicate == {"outcome": "existing", "attachment": added["attachment"]}
    assert repository.get_conversation_summary(first.id).revision == revision
    repository.thread_resources.add(first.id, "note", "resource-2", ["actual", "JSON", 2])
    page = repository.thread_resources.list(first.id, limit=1)
    assert page["next_cursor"]
    assert len(repository.thread_resources.list(first.id, cursor=page["next_cursor"], limit=1)["data"]) == 1
    with pytest.raises(ValueError, match="pagination cursor"):
        repository.thread_resources.list(second.id, cursor=page["next_cursor"], limit=1)
    repository.thread_resources.add(second.id, "pull_request", identity, payload)
    repository.set_archived(second.id, True)
    owners = repository.thread_resources.owners("pull_request", identity, limit=1)
    assert owners["next_cursor"]
    with pytest.raises(ValueError, match="pagination cursor"):
        repository.thread_resources.owners("pull_request", identity, archived=True, cursor=owners["next_cursor"])
    assert repository.thread_resources.owners("pull_request", identity, archived=True)["data"] == [{"thread_id": second.id, "archived": True}]
    repository.clear_conversation(first.id)
    clone = repository.clone_conversation(first.id)
    source_refs = repository.thread_resources.list(first.id)["data"]
    clone_refs = repository.thread_resources.list(clone.id)["data"]
    assert {entry["identity_key"] for entry in clone_refs} == {entry["identity_key"] for entry in source_refs}
    assert {entry["id"] for entry in clone_refs}.isdisjoint(entry["id"] for entry in source_refs)
    cold = ConversationRepository(repository.storage_root)
    assert cold.thread_resources.list(first.id)["data"] == source_refs
    cold.thread_resources.remove(first.id, "pull_request", identity)
    assert first.id not in {entry["thread_id"] for entry in cold.thread_resources.owners("pull_request", identity)["data"]}
    cold.delete_conversation(second.id)
    assert second.id not in {entry["thread_id"] for entry in cold.thread_resources.owners("pull_request", identity)["data"]}


def test_worktree_bindings_and_registrations_are_real_resource_membership(stores, tmp_path):
    repository, _sessions, _app = stores
    root, worktree, restored = (tmp_path / name for name in ("repo", "worktree", "restored"))
    record = repository.create_conversation(workspace_root=str(root), worktree_path=str(worktree), git_isolated=True)
    reference = repository.thread_resources.list(record.id)["data"][0]
    assert reference["attachment_type"] == "worktree"
    assert reference["payload"]["root"] == str(worktree)
    assert reference["payload"]["workspaceRoot"] == str(root)
    repository.update_workspace_binding(record.id, workspace_root=str(root), worktree_path="", git_isolated=False)
    assert repository.thread_resources.list(record.id)["data"] == []
    repository.register_worktree(record.id, path=str(restored), backend="git", workspace_root=str(root))
    repository.register_worktree(record.id, path=str(restored), backend="git", workspace_root=str(root))
    assert len(repository.thread_resources.list(record.id)["data"]) == 1
    repository.unregister_worktree(record.id, path=str(restored))
    assert repository.thread_resources.list(record.id)["data"] == []


def test_source_resource_capacity_and_utf8_bounds_are_enforced_at_the_shared_store_entry(stores):
    repository, _sessions, _app = stores
    record = repository.create_conversation()
    with pytest.raises(ValueError, match="256 bytes"):
        repository.thread_resources.add(record.id, "中" * 86, "key", {})
    with pytest.raises(ValueError, match="256 bytes"):
        repository.thread_resources.add(record.id, "type", "中" * 86, {})
    with pytest.raises(ValueError, match="65536 bytes"):
        repository.thread_resources.add(record.id, "type", "key", "中" * 22000)
    references = [resource_reference("type", f"key-{index}", {"index": index}) for index in range(100)]
    repository._mutate_meta(record.id, lambda current: setattr(current, "resource_references", references))
    assert repository.thread_resources.add(record.id, "type", "key-0", {})["outcome"] == "existing"
    with pytest.raises(ValueError, match="at most 100"):
        repository.thread_resources.add(record.id, "type", "overflow", {})
    repository.thread_resources.remove(record.id, "type", "key-0")
    assert repository.thread_resources.add(record.id, "type", "replacement", {})["outcome"] == "created"


@pytest.mark.asyncio
async def test_project_section_move_unpins_durably_and_publishes_the_real_project_and_section_inventories(stores, tmp_path):
    repository, sessions, app = stores
    root, secondary = tmp_path / "project", tmp_path / "secondary"
    root.mkdir()
    secondary.mkdir()
    store = RecentProjectStore()
    project = store.create("Two sources", [str(root), str(secondary)])
    chat = repository.create_conversation(workspace_root=str(secondary), execution_cwd=str(secondary),
        context_snapshot={"original": "keep hidden workspace checkpoint"}, transcript=[{"id": "u", "role": "user", "content": "Saved task"}])
    before = chat.to_dict()
    section = repository.thread_sections.create("Source section")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://metadata") as client:
        stale = await client.post("/api/thread-metadata/sections/move", params={"session_id": "original-session"},
            json={"kind": "project", "id": project.path, "section_id": "removed-section"})
        assert stale.status_code == 409, stale.text
        assert RecentProjectStore().list()[0].pinned is True
        assert repository.thread_sections.list()[0]["item_keys"] == []
        for session in sessions.values():
            session.send_payload.assert_not_awaited()
        result = await client.post("/api/thread-metadata/sections/move", params={"session_id": "original-session"},
            json={"kind": "project", "id": project.path, "section_id": section["id"]})
        assert result.status_code == 200, result.text
        cold = ConversationRepository(repository.storage_root)
        assert cold.thread_sections.list()[0]["item_keys"] == ["project:" + canonical_file_path_key(project.path)]
        assert RecentProjectStore().list()[0].pinned is False
        assert cold.get_conversation(chat.id).to_dict() == before
        for session in sessions.values():
            payload = session.send_payload.await_args.args[0]
            assert payload["type"] == "workspace.recent.list"
            assert payload["projects"][0]["pinned"] is False
            assert payload["projects"][0]["source_folders"] == [str(root), str(secondary)]
        sessions["original-session"].send_conversation_list.assert_awaited_once()
        sessions["same-store-window"].send_conversation_list.assert_awaited_once()
        sessions["other-store-window"].send_conversation_list.assert_not_awaited()
        removed = await client.post("/api/thread-metadata/sections/move", params={"session_id": "original-session"},
            json={"kind": "project", "id": project.path, "section_id": None})
        assert removed.status_code == 200, removed.text
    assert RecentProjectStore().list()[0].pinned is False
    assert ConversationRepository(repository.storage_root).thread_sections.list()[0]["item_keys"] == []
    assert ConversationRepository(repository.storage_root).get_conversation(chat.id).to_dict() == before


@pytest.mark.asyncio
async def test_actual_http_sections_preserve_appearance_and_broadcast_only_the_owning_store(stores):
    repository, sessions, app = stores
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://metadata") as client:
        query = {"session_id": "original-session"}
        created = await client.post("/api/thread-metadata/sections", params=query,
            json={"name": "First", "appearance": {"icon": "中" * 21, "color": "blue"}})
        assert created.status_code == 200, created.text
        section = created.json()["section"]
        assert (await client.patch(f"/api/thread-metadata/sections/{section['id']}", params=query,
            json={"name": "Renamed"})).json()["section"]["appearance"] == section["appearance"]
        assert (await client.patch(f"/api/thread-metadata/sections/{section['id']}", params=query,
            json={"name": "Renamed", "appearance": None})).json()["section"]["appearance"] is None
        assert (await client.post("/api/thread-metadata/sections", params=query,
            json={"name": "No truncated UTF8", "appearance": {"color": "中" * 22}})).status_code == 422
        assert (await client.post("/api/thread-metadata/sections", params=query, json={"name": "   "})).status_code == 409
        assert (await client.get("/api/thread-metadata/sections", params={"session_id": "retired"})).status_code == 404
        first = repository.create_conversation()
        moved = await client.post("/api/thread-metadata/sections/move", params=query,
            json={"kind": "thread", "id": first.id, "section_id": section["id"]})
        assert moved.status_code == 200, moved.text
        assert (await client.get("/api/thread-metadata/sections", params=query)).headers["cache-control"] == "no-store"
    sessions["original-session"].send_conversation_list.assert_awaited()
    sessions["same-store-window"].send_conversation_list.assert_awaited()
    sessions["other-store-window"].send_conversation_list.assert_not_awaited()


@pytest.mark.parametrize("url,expected", [
    ("https://github.com/Owner/Repo/pull/2/files?x=1", '["github.com","owner","repo",2]'),
    ("https://gitlab.com/group/subgroup/project/-/merge_requests/9/diffs", '["gitlab.com","group/subgroup","project",9]'),
    ("https://gitlab.example/Group/Repo/-/merge_requests/4", '["gitlab.example","group","repo",4]'),
])
def test_source_pull_request_identity_and_canonical_payload(url, expected):
    identity, payload = pull_request_resource(url)
    assert identity == expected
    assert pull_request_identity(payload["url"]) == identity
    assert "?" not in payload["url"]


@pytest.mark.parametrize("url", ["https://github.com/a/b/pull/0", "https://github.com/a%2Fb/c/pull/1",
    "file:///a/b/pull/1", "http://gitlab.com/a/b/-/merge_requests/3", "https://gitlab.com/a/../b/-/merge_requests/4"])
def test_invalid_source_resource_url_is_a_tool_error(url):
    with pytest.raises(ValueError):
        pull_request_identity(url)


@pytest.mark.asyncio
async def test_artifact_tools_use_original_repository_even_while_another_chat_is_selected(stores):
    repository, sessions, _app = stores
    first, second = repository.create_conversation(), repository.create_conversation()
    sessions["original-session"].active_conversation_id = second.id
    context = SimpleNamespace(conversation_id=first.id, run_context=SimpleNamespace(conversation_repository=repository))
    args = {"artifact_type": "pull_request", "url": "https://GitHub.com/Owner/Repo/pull/8/files"}
    attached = await ThreadResourceTool("attach_artifact").execute(args, context)
    assert not attached.is_error
    receipt = json.loads(attached.content)
    assert receipt["attachment"]["payload"]["url"] == "https://github.com/owner/repo/pull/8"
    listed = json.loads((await ThreadResourceTool("list_artifacts").execute({}, context)).content)
    assert len(listed["data"]) == 1
    assert repository.thread_resources.list(second.id)["data"] == []
    assert not (await ThreadResourceTool("remove_artifact").execute(args, context)).is_error
    assert repository.thread_resources.list(first.id)["data"] == []


def _pr_body(url, state="OPEN", draft=False):
    return json.dumps({"number": 8, "title": "Actual upstream title", "state": state, "isDraft": draft,
        "url": url, "headRefName": "original-branch", "statusCheckRollup": []}).encode()


@pytest.mark.asyncio
async def test_pr_query_runs_explicit_saved_url_without_using_the_selected_chat_branch(stores, monkeypatch):
    repository, sessions, app = stores
    first, second = repository.create_conversation(), repository.create_conversation()
    identity, payload = pull_request_resource("https://github.enterprise/owner/repo/pull/8")
    resource = repository.thread_resources.add(first.id, "pull_request", identity, payload)["attachment"]
    sessions["original-session"].active_conversation_id = second.id
    process = SimpleNamespace(returncode=0)
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(pr_service, "github_cli_command", lambda: "original-host-gh")
    monkeypatch.setattr(pr_service, "spawn_exec", spawn)
    monkeypatch.setattr(pr_service, "communicate", AsyncMock(return_value=(_pr_body(payload["url"]), b"")))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://metadata") as client:
        result = await client.get(f"/api/thread-metadata/{first.id}/resources/{resource['id']}/pull-request",
            params={"session_id": "original-session"})
    assert result.status_code == 200, result.text
    assert result.json()["conversation_id"] == first.id
    assert result.json()["pr"]["headRefName"] == "original-branch"
    assert spawn.call_args.args[:4] == ("original-host-gh", "pr", "view", payload["url"])
    assert spawn.call_args.kwargs["cwd"] == str(repository.storage_root)


@pytest.mark.asyncio
@pytest.mark.parametrize("gitlab", [False, True])
async def test_pr_query_borrows_and_closes_original_remote_executor(stores, monkeypatch, gitlab):
    from backend.workspace import executor_commands, executor_editor

    repository, sessions, _app = stores
    first = repository.create_conversation()
    identity, payload = pull_request_resource("https://gitlab.enterprise/group/repo/-/merge_requests/8" if gitlab else "https://github.com/owner/repo/pull/8")
    resource = repository.thread_resources.add(first.id, "pull_request", identity, payload)["attachment"]
    closed = []
    context = SimpleNamespace(turn_environment=SimpleNamespace(is_remote=True))
    service = SimpleNamespace(original_owner=first.id)

    @asynccontextmanager
    async def capture(session, owner):
        assert owner == first.id and session is sessions["original-session"]
        try:
            yield context
        finally:
            closed.append(owner)

    class Editor:
        def __init__(self, current, application_config): assert current is context
        async def __aenter__(self): return service
        async def __aexit__(self, *_): pass

    monkeypatch.setattr(pr_service, "capture_workspace_execution", capture)
    sessions["original-session"].config = object()
    monkeypatch.setattr(executor_editor, "ExecutorEditorService", Editor)
    monkeypatch.setattr(executor_commands, "target_programs", AsyncMock(return_value={"gh": "/remote/bin/gh", "glab": "/remote/bin/glab"}))
    body = json.dumps({"iid": 8, "title": "Remote GL", "state": "locked", "draft": True,
        "web_url": payload["url"], "source_branch": "original-branch"}) if gitlab else _pr_body(payload["url"]).decode()
    run = AsyncMock(return_value=SimpleNamespace(stdout=body))
    monkeypatch.setattr(executor_commands, "run_workbench_command", run)
    result = await pr_service.linked_pull_request_status(sessions["original-session"], first.id, resource["id"])
    assert result["pr"]["url"] == payload["url"]
    expected = ["/remote/bin/glab", "api", "--hostname", "gitlab.enterprise", "projects/group%2Frepo/merge_requests/8"] if gitlab else ["/remote/bin/gh", "pr", "view", payload["url"], "--json", pr_service._FIELDS]
    assert run.call_args.args == (service, expected)
    assert closed == [first.id]


@pytest.mark.asyncio
@pytest.mark.parametrize("body,returncode,status", [(b"", 1, 502), (b"not JSON", 0, 502),
    (_pr_body("https://github.com/different/repo/pull/8"), 0, 502)])
async def test_real_upstream_failures_remain_failures_and_never_rewrite_membership(stores, monkeypatch, body, returncode, status):
    from fastapi import HTTPException

    repository, sessions, _app = stores
    first = repository.create_conversation()
    identity, payload = pull_request_resource("https://github.com/owner/repo/pull/8")
    resource = repository.thread_resources.add(first.id, "pull_request", identity, payload)["attachment"]
    before = repository.get_conversation_summary(first.id).revision
    monkeypatch.setattr(pr_service, "github_cli_command", lambda: "actual-gh")
    monkeypatch.setattr(pr_service, "spawn_exec", AsyncMock(return_value=SimpleNamespace(returncode=returncode)))
    monkeypatch.setattr(pr_service, "communicate", AsyncMock(return_value=(body, b"permission denied")))
    with pytest.raises(HTTPException) as error:
        await pr_service.linked_pull_request_status(sessions["original-session"], first.id, resource["id"])
    assert error.value.status_code == status
    assert repository.get_conversation_summary(first.id).revision == before


@pytest.mark.asyncio
async def test_gitlab_link_is_preserved_when_its_host_cli_is_unavailable(stores, monkeypatch):
    from fastapi import HTTPException

    repository, sessions, _app = stores
    first = repository.create_conversation()
    identity, payload = pull_request_resource("https://gitlab.com/group/repo/-/merge_requests/8")
    resource = repository.thread_resources.add(first.id, "pull_request", identity, payload)["attachment"]
    monkeypatch.setattr(pr_service.shutil, "which", lambda _program: None)
    with pytest.raises(HTTPException) as error:
        await pr_service.linked_pull_request_status(sessions["original-session"], first.id, resource["id"])
    assert error.value.status_code == 503
    assert repository.thread_resources.list(first.id)["data"] == [resource]


@pytest.mark.asyncio
@pytest.mark.parametrize("state,draft,projected", [("opened", False, "OPEN"), ("locked", True, "OPEN"), ("merged", False, "MERGED"), ("closed", False, "CLOSED")])
async def test_gitlab_cli_uses_the_saved_host_and_encoded_project_with_source_state_mapping(stores, monkeypatch, state, draft, projected):
    repository, sessions, _app = stores
    first = repository.create_conversation()
    identity, payload = pull_request_resource("https://gitlab.enterprise/group/subgroup/repo/-/merge_requests/8")
    resource = repository.thread_resources.add(first.id, "pull_request", identity, payload)["attachment"]
    body = json.dumps({"iid": 8, "title": "Original GitLab MR", "state": state, "draft": draft,
        "web_url": payload["url"], "source_branch": "original-branch"}).encode()
    spawn = AsyncMock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(pr_service.shutil, "which", lambda program: "original-glab" if program == "glab" else None)
    monkeypatch.setattr(pr_service, "spawn_exec", spawn)
    monkeypatch.setattr(pr_service, "communicate", AsyncMock(return_value=(body, b"")))
    result = await pr_service.linked_pull_request_status(sessions["original-session"], first.id, resource["id"])
    assert spawn.call_args.args == ("original-glab", "api", "--hostname", "gitlab.enterprise", "projects/group%2Fsubgroup%2Frepo/merge_requests/8")
    assert result["pr"]["state"] == projected and result["pr"]["isDraft"] == draft
    assert result["pr"]["url"] == payload["url"]
