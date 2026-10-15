from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.loop_session import populate_prompt_context
from backend.agent.run_context import RunContext
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.attachments.store import AttachmentStore
from backend.conversations.models import ConversationRecord, ConversationSummary, inherited_execution_cwd
from backend.conversations.public_projection import project_public_conversation
from backend.conversations.repository import ConversationRepository
from backend.config import PermissionSettings
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.services.chat_api_service import reserve_attachment_upload_context
from backend.services.projectless_directory_service import create_projectless_directory
from backend.services.workspace_service import conversation_workspace_path, resolve_requested_workspace
from backend.tools.command_tool import RunCommandTool
from backend.tools.read_file import ReadFileTool
from backend.tools.write_file import WriteFileTool
from backend.terminal.session import TerminalSessionManager
from backend.workspace import trust
from backend.ws.command_scope import resolve_command_scope
from backend.ws.session_lifecycle import SessionLifecycle
from backend.ws.handlers.terminal import handle_terminal_create, handle_terminal_input, handle_terminal_restart


@pytest.fixture
def trusted_ledger(tmp_path, monkeypatch):
    ledger = tmp_path / "desktop-state" / "trusted_workspaces.json"
    monkeypatch.setattr(trust, "TRUSTED_WORKSPACES_FILE", ledger)
    return ledger


def owner_session(repository, conversation_id, tmp_path):
    session = SimpleNamespace(active_conversation_id=conversation_id, conversation_repo=repository,
        attachment_store=AttachmentStore(tmp_path / "attachments"), artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
        _attachment_upload_lock=__import__("threading").RLock())
    session.session_lifecycle = SessionLifecycle(session)
    mounted = repository.get_conversation(conversation_id).execution_cwd
    session.session_lifecycle.workspace_root = Path(mounted) if mounted else None
    session.resolve_requested_workspace = lambda requested=None: resolve_requested_workspace(session.session_lifecycle.current_workspace_root(), requested)
    return session


def test_exact_projectless_directory_shape_collision_and_task_only_trust(tmp_path, trusted_ledger):
    original = tmp_path / "original-project"
    original.mkdir()
    trusted_ledger.parent.mkdir()
    trusted_ledger.write_text(json.dumps({"roots": [str(original)], "other": "keep"}), encoding="utf-8")
    base = tmp_path / "Documents" / "MiniCode"
    first = create_projectless_directory(str(base), "Read THE file and write final outputs more words", date=datetime(2026, 10, 13))
    second = create_projectless_directory(str(base), "Read THE file and write final outputs more words", date=datetime(2026, 10, 13))
    chinese = create_projectless_directory(str(base), "分析一下", date=datetime(2026, 10, 13))
    assert Path(first.cwd) == base / "2026-10-13" / "read-the-file-and-write-final"
    assert Path(second.cwd).name == "read-the-file-and-write-final-2"
    assert Path(chinese.cwd).name == "new-chat"
    assert Path(first.output_directory).is_dir() and (Path(first.cwd) / "work").is_dir()
    assert first.browser_root == str(base)
    ledger = json.loads(trusted_ledger.read_text(encoding="utf-8"))
    assert ledger["other"] == "keep" and str(original) in ledger["roots"]
    assert str(base) not in ledger["roots"]
    assert trust.is_workspace_trusted(Path(first.cwd))
    assert not trust.is_workspace_trusted(base)


def test_numeric_collisions_use_official_five_uuid_suffix_attempts(tmp_path, trusted_ledger, monkeypatch):
    base = tmp_path / "tasks"
    day = base / "2026-10-13"
    day.mkdir(parents=True)
    for index in range(100):
        (day / ("same" if index == 0 else f"same-{index + 1}")).mkdir()
    monkeypatch.setattr("backend.services.projectless_directory_service.uuid4", lambda: "actual-unique-suffix")
    created = create_projectless_directory(str(base), "same", date=datetime(2026, 10, 13))
    assert Path(created.cwd).name == "same-actual-unique-suffix"


def test_repository_owns_directory_mint_replay_and_keeps_project_assignment_empty(tmp_path, trusted_ledger):
    repository = ConversationRepository(tmp_path / "conversations")
    base = tmp_path / "tasks"
    created = repository.create_conversation(creation_command_id="window:create-1", projectless_base=str(base), initial_prompt="create report")
    metadata = project_public_conversation(created)
    assert metadata["workspace_root"] == ""
    assert conversation_workspace_path(created) == ""
    assert Path(metadata["execution_cwd"]).parent.parent == base
    assert metadata["projectless_output_dir"] == str(Path(created.execution_cwd) / "outputs")
    assert metadata["projectless_browser_root"] == str(base)
    replayed = repository.create_conversation(creation_command_id="window:create-1", projectless_base=str(tmp_path / "new-default"), initial_prompt="changed")
    assert replayed.id == created.id and replayed.execution_cwd == created.execution_cwd
    assert not (tmp_path / "new-default").exists()
    reopened = ConversationRepository(tmp_path / "conversations").get_conversation(created.id)
    assert reopened.execution_cwd == created.execution_cwd and reopened.workspace_root == ""
    assert reopened.to_summary().projectless_browser_root == str(base)


def test_fixed_cwd_flows_to_scope_real_read_write_command_and_attachments(tmp_path, trusted_ledger):
    repository = ConversationRepository(tmp_path / "conversations")
    created = repository.create_conversation(projectless_base=str(tmp_path / "tasks"), initial_prompt="verify owner", permission_mode="bypass")
    session = owner_session(repository, created.id, tmp_path)
    scope = resolve_command_scope(session, {"conversation_id": created.id}, require_workspace=True)
    root = Path(created.execution_cwd)
    assert scope.workspace_root == str(root)
    assert session.session_lifecycle.workspace_root_for_conversation(created) == root
    assert session.session_lifecycle.current_workspace_root() == root
    checker = PermissionChecker(PermissionSettings(), workspace_root=root)
    context = ToolExecutionContext(permission=PermissionContext(mode="bypass", workspace_root=root), workspace_root=root,
        conversation_id=created.id, session_id="session", permission_checker=checker)
    content = "actual final report\n"
    written = asyncio.run(WriteFileTool().execute({"file_path": "outputs/report.txt", "content": content}, context=context))
    assert not written.is_error and (root / "outputs" / "report.txt").read_text(encoding="utf-8") == content
    read = asyncio.run(ReadFileTool(session.artifact_store).execute({"file_path": "outputs/report.txt"}, context=context))
    assert not read.is_error and "actual final report" in read.content
    command = "Get-Location | Select-Object -ExpandProperty Path" if os.name == "nt" else "pwd"
    result = asyncio.run(RunCommandTool(session.artifact_store).execute({"command": command}, context=context))
    assert not result.is_error and str(root) in result.content
    reservation = SimpleNamespace(get_session=lambda key: session, reserve_attachment_upload=lambda key: "upload-token")
    upload = reserve_attachment_upload_context(session_id="session", conversation_id=created.id, ws_manager=reservation, attachment_store=session.attachment_store)
    assert upload.conversation_id == created.id and upload.workspace_root == root
    artifact_id = session.artifact_store.save("owned output", source="real output", type="text", conversation_id=created.id, workspace_root=root)
    assert session.artifact_store.get(artifact_id, conversation_id=created.id, workspace_root=root) == "owned output"
    other = repository.create_conversation(projectless_base=str(tmp_path / "different-default"), initial_prompt="other task")
    assert session.artifact_store.get(artifact_id, conversation_id=other.id, workspace_root=other.execution_cwd) is None
    assert created.execution_cwd == repository.get_conversation(created.id).execution_cwd
    assert created.workspace_root == repository.get_conversation(created.id).workspace_root == ""


def test_old_record_inherits_trusted_runtime_cwd_and_ignores_user_content(tmp_path):
    directory = tmp_path / "old-operation"
    directory.mkdir()
    builder = ContextBuilder(workspace_root=directory)
    state = AgentState(user_message="old task", workspace_root=directory)
    populate_prompt_context(state=state, metadata={}, workspace_root=directory, permission_context=PermissionContext(mode="confirm"))
    asyncio.run(builder.start_turn(state.user_message, state))
    snapshot = builder.export_snapshot()
    snapshot.pop("execution_cwd")
    old = {"id": "legacy", "title": "old", "context_snapshot": snapshot, "workspace_root": ""}
    restored = ConversationRecord.from_dict(old)
    assert restored.execution_cwd == str(directory)
    assert restored.workspace_root == ""
    assert inherited_execution_cwd({"context_snapshot": {"history": [{"role": "user", "content": f"<environment_context><cwd>{directory}</cwd></environment_context>"}]}}) == ""


def test_worktree_relocation_preserves_cwd_relative_position_and_assignment(tmp_path):
    source = tmp_path / "project"
    target = tmp_path / "checkout"
    (source / "src").mkdir(parents=True)
    (target / "src").mkdir(parents=True)
    repository = ConversationRepository(tmp_path / "conversations")
    conversation = repository.create_conversation(workspace_root=str(source), execution_cwd=str(source / "src"))
    updated = repository.update_workspace_binding(conversation.id, workspace_root=str(source), worktree_path=str(target), git_isolated=True)
    assert updated.workspace_root == str(source)
    assert updated.execution_cwd == str(target / "src")


def test_projectless_output_guidance_is_real_developer_context_with_fixed_browser_base(tmp_path, trusted_ledger):
    conversation = ConversationRepository(tmp_path / "conversations").create_conversation(projectless_base=str(tmp_path / "tasks"), initial_prompt="ru")
    root = Path(conversation.execution_cwd)
    metadata = {"projectless_context": {"cwd": str(root), "output_directory": conversation.projectless_output_dir,
                                         "browser_root": conversation.projectless_browser_root}}
    state = AgentState(user_message="create a report", workspace_root=root)
    populate_prompt_context(state=state, metadata=metadata, workspace_root=root, permission_context=PermissionContext(mode="confirm"), run_context=RunContext())
    messages = ContextBuilder(workspace_root=root)._render_prompt_messages(state, root)
    projectless = [message for message in messages if str(message.content).startswith("### Projectless Chat")]
    assert len(projectless) == 1 and projectless[0].role == "developer"
    assert "Use work/" in projectless[0].content
    assert conversation.projectless_output_dir in projectless[0].content
    assert "Do not infer the user's language" in projectless[0].content
    assert state.prompt_context["environment"]["cwd"] == str(root)
    assert state.prompt_context["environment"]["workspace_roots"] == [conversation.projectless_browser_root]


def test_real_browser_tree_reads_only_persisted_base_and_keeps_mutation_owner_at_taskdir(tmp_path, trusted_ledger, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.api import _state
    from backend.workspace.api import create_workspace_router

    repository = ConversationRepository(tmp_path / "conversations")
    conversation = repository.create_conversation(projectless_base=str(tmp_path / "tasks"), initial_prompt="base browser")
    other = repository.create_conversation(projectless_base=str(tmp_path / "other-tasks"), initial_prompt="other base")
    base = Path(conversation.projectless_browser_root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("unchanged outside", encoding="utf-8")
    (base / "existing.txt").write_text("unchanged base", encoding="utf-8")
    png = __import__("base64").b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a1ioAAAAASUVORK5CYII=")
    (base / "pixel.png").write_bytes(png)
    session = owner_session(repository, conversation.id, tmp_path)
    monkeypatch.setattr(_state, "ws_manager", SimpleNamespace(get_session=lambda sid: session if sid == "browser-session" else None))
    app = FastAPI()
    app.include_router(create_workspace_router())
    owner = {"session_id": "browser-session", "conversation_id": conversation.id, "execution_cwd": conversation.execution_cwd}
    with TestClient(app) as client:
        listed = client.get("/api/workspace/browser-tree", params={**owner, "path": "."})
        assert listed.status_code == 200
        assert listed.json()["workspace_root"] == str(base)
        assert "existing.txt" in [entry["name"] for entry in listed.json()["entries"]]
        read = client.get("/api/workspace/browser-file", params={**owner, "path": "existing.txt"})
        assert read.status_code == 200 and read.json()["content"] == "unchanged base"
        preview = client.get("/api/workspace/browser-preview", params={**owner, "path": "existing.txt"})
        assert preview.status_code == 200 and preview.json()["content"] == "unchanged base"
        raw = client.get("/api/workspace/browser-raw", params={**owner, "path": "existing.txt"})
        assert raw.status_code == 200 and raw.content == b"unchanged base"
        image_preview = client.get("/api/workspace/browser-preview", params={**owner, "path": "pixel.png"})
        assert image_preview.status_code == 200 and image_preview.json()["has_native"] is True
        assert image_preview.json()["media_type"] == "image/png"
        image_raw = client.get("/api/workspace/browser-raw", params={**owner, "path": "pixel.png"})
        assert image_raw.status_code == 200 and image_raw.content == png
        assert image_raw.headers["content-type"] == "image/png"
        nested = client.get("/api/workspace/browser-tree", params={**owner, "path": Path(conversation.execution_cwd).relative_to(base).as_posix()})
        assert nested.status_code == 200
        assert {entry["name"] for entry in nested.json()["entries"]} == {"outputs", "work"}
        assert client.get("/api/workspace/tree", params={"workspace_root": str(base)}).status_code == 403
        assert client.get("/api/workspace/browser-tree", params={**owner, "path": "../outside"}).status_code == 400
        assert client.get("/api/workspace/browser-tree", params={**owner, "conversation_id": other.id}).status_code == 409
        assert client.get("/api/workspace/browser-tree", params={**owner, "execution_cwd": other.execution_cwd}).status_code == 409
        assert client.get("/api/workspace/browser-tree", params={**owner, "session_id": "disconnected"}).status_code == 404
        for endpoint in ("browser-file", "browser-preview", "browser-raw"):
            assert client.get(f"/api/workspace/{endpoint}", params={**owner, "path": "../outside/private.txt"}).status_code == 400
            assert client.get(f"/api/workspace/{endpoint}", params={**owner, "execution_cwd": other.execution_cwd, "path": "existing.txt"}).status_code == 409
        assert client.put("/api/workspace/file", params={"workspace_root": str(base)}, json={"path": "existing.txt", "content": "must not write"}).status_code == 403
    assert (base / "existing.txt").read_text(encoding="utf-8") == "unchanged base"
    assert (outside / "private.txt").read_text(encoding="utf-8") == "unchanged outside"
    assert repository.get_conversation(conversation.id).execution_cwd == conversation.execution_cwd
    assert not trust.is_workspace_trusted(base)


def test_real_browser_tree_project_root_is_actual_cwd_and_unbound_browsing_never_binds(tmp_path, trusted_ledger, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.api import _state
    from backend.workspace.api import create_workspace_router

    project = tmp_path / "project"
    actual_directory = project / "src"
    actual_directory.mkdir(parents=True)
    (actual_directory / "main.py").write_bytes(b"print('actual owner')\n")
    (project / "outside-cwd.txt").write_text("project parent", encoding="utf-8")
    trust.trust_created_task_directory(project)
    repository = ConversationRepository(tmp_path / "conversations")
    conversation = repository.create_conversation(workspace_root=str(project), execution_cwd=str(actual_directory))
    unbound = repository.create_conversation()
    session = owner_session(repository, conversation.id, tmp_path)
    monkeypatch.setattr(_state, "ws_manager", SimpleNamespace(get_session=lambda sid: session))
    app = FastAPI()
    app.include_router(create_workspace_router())
    owner = {"session_id": "browser-project", "conversation_id": conversation.id, "execution_cwd": str(actual_directory)}
    with TestClient(app) as client:
        listed = client.get("/api/workspace/browser-tree", params=owner)
        assert listed.status_code == 200
        assert listed.json()["workspace_root"] == str(actual_directory)
        assert [entry["path"] for entry in listed.json()["entries"]] == ["main.py"]
        read = client.get("/api/workspace/browser-file", params={**owner, "path": "main.py"})
        assert read.status_code == 200 and read.json()["content"] == "print('actual owner')\n"
        assert client.get("/api/workspace/browser-file", params={**owner, "path": "../outside-cwd.txt"}).status_code == 400
        session.active_conversation_id = unbound.id
        unknown = client.get("/api/workspace/browser-tree", params={**owner, "conversation_id": unbound.id})
        assert unknown.status_code == 409
    assert repository.get_conversation(unbound.id).execution_cwd == ""
    assert repository.get_conversation(conversation.id).workspace_root == str(project)


def test_imported_browser_metadata_has_no_base_capability_without_matching_local_mint(tmp_path, trusted_ledger):
    from backend.conversations.import_export import import_conversation_tree

    repository = ConversationRepository(tmp_path / "conversations")
    original = repository.create_conversation(projectless_base=str(tmp_path / "tasks"), initial_prompt="local owner")
    payload = {"schema": "minicode.conversation.export", "version": 1, "selected_conversation_id": original.id,
               "conversations": [project_public_conversation(original)]}
    imported_local = import_conversation_tree(repository, payload)
    local_copy = repository.get_conversation(imported_local["conversation_id"])
    assert local_copy.projectless_browser_root == original.projectless_browser_root
    assert local_copy.projectless_output_dir == original.projectless_output_dir
    foreign = ConversationRepository(tmp_path / "foreign-conversations")
    imported_foreign = import_conversation_tree(foreign, payload)
    foreign_copy = foreign.get_conversation(imported_foreign["conversation_id"])
    assert foreign_copy.execution_cwd == original.execution_cwd
    assert foreign_copy.projectless_browser_root == foreign_copy.projectless_output_dir == ""
    forged_payload = json.loads(json.dumps(payload))
    forged_payload["conversations"][0]["projectless_browser_root"] = str(tmp_path)
    forged = import_conversation_tree(repository, forged_payload)
    assert repository.get_conversation(forged["conversation_id"]).projectless_browser_root == ""


def test_production_browser_raw_signed_url_authorizes_exact_owner_and_route(tmp_path, trusted_ledger, monkeypatch):
    import base64
    import hashlib
    import hmac
    import time
    from fastapi.testclient import TestClient
    from backend.api import _state
    from backend.main import app

    secret = "browser-contract-audit-runtime"
    monkeypatch.setenv("MINICODE_RUNTIME_TOKEN", secret)
    repository = ConversationRepository(tmp_path / "conversations")
    conversation = repository.create_conversation(projectless_base=str(tmp_path / "tasks"), initial_prompt="native raw asset")
    target = Path(conversation.projectless_browser_root) / "report.txt"
    target.write_bytes(b"actual signed asset")
    session = owner_session(repository, conversation.id, tmp_path)
    monkeypatch.setattr(_state, "ws_manager", SimpleNamespace(get_session=lambda sid: session if sid == "signed-session" else None))
    owner = {"session_id": "signed-session", "conversation_id": conversation.id, "execution_cwd": conversation.execution_cwd, "path": "report.txt"}

    def signed(params, expires):
        # This is the frontend contract's literal UTF-8 payload, independent
        # of the backend signature helper used by production middleware.
        body = f"browser_raw:v1:{params['session_id']}:{params['conversation_id']}:{params['execution_cwd']}:{params['path']}:{expires}".encode("utf-8")
        signature = base64.urlsafe_b64encode(hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()).decode("ascii").rstrip("=")
        return {**params, "raw_token": f"{expires}.{signature}"}

    expires = int(time.time()) + 300
    url_params = signed(owner, expires)
    client = TestClient(app)
    try:
        assert client.get("/api/workspace/browser-raw", params=owner).status_code == 401
        raw = client.get("/api/workspace/browser-raw", params=url_params)
        assert raw.status_code == 200 and raw.content == b"actual signed asset"
        assert secret not in str(raw.request.url)
        assert client.get("/api/v1/workspace/browser-raw", params=url_params).content == b"actual signed asset"
        assert client.get("/api/workspace/browser-preview", params=url_params).status_code == 401
        preview = client.get("/api/workspace/browser-preview", params=owner, headers={"x-minicode-token": secret})
        assert preview.status_code == 200 and preview.json()["content"] == "actual signed asset"
        assert client.get("/api/workspace/browser-raw", params={**url_params, "path": "different.txt"}).status_code == 401
        assert client.get("/api/workspace/browser-raw", params={**url_params, "execution_cwd": str(tmp_path)}).status_code == 401
        stale = {**owner, "conversation_id": "conv_other_owner"}
        assert client.get("/api/workspace/browser-raw", params=signed(stale, expires)).status_code == 409
        assert client.get("/api/workspace/browser-raw", params=signed(owner, int(time.time()) - 1)).status_code == 401
        assert client.get("/api/workspace/raw", params={**url_params, "workspace_root": conversation.execution_cwd}).status_code == 401
        target.write_bytes(b"updated signed asset")
        assert client.get("/api/workspace/browser-raw", params=url_params).content == b"updated signed asset"
    finally:
        client.close()


def test_legacy_empty_attachment_scope_is_retained_when_real_execution_owner_is_fixed(tmp_path):
    directory = tmp_path / "actual-original-operation"
    directory.mkdir()
    snapshot = {"history": [{"role": "user", "content": "original request", "runtime_context": f"<environment_context><cwd>{directory}</cwd></environment_context>"}]}
    legacy_payload = {"id": "conv_legacy_owner", "title": "original", "workspace_root": "", "context_snapshot": snapshot}
    repository = ConversationRepository(tmp_path / "conversations")
    repository.save_conversation(ConversationRecord.from_dict(legacy_payload))
    session = owner_session(repository, "conv_legacy_owner", tmp_path)
    attachment_id = "attachment-original"
    session.attachment_store.save(artifact_id=attachment_id, content="original uploaded file", metadata={"conversation_id": "conv_legacy_owner", "workspace_root": ""})
    artifact_id = session.artifact_store.save("original output", source="original", type="text", conversation_id="conv_legacy_owner", workspace_root="")
    old_metadata = ConversationSummary.from_dict({"id": "conv_legacy_owner", "title": "original", "workspace_root": ""})
    assert session.session_lifecycle.workspace_root_for_conversation(old_metadata) == directory
    assert session.attachment_store.get(attachment_id, conversation_id="conv_legacy_owner", workspace_root=directory) is None
    session.session_lifecycle.bind_execution_for_operation(old_metadata)
    assert session.attachment_store.get(attachment_id, conversation_id="conv_legacy_owner", workspace_root=directory) == "original uploaded file"
    assert session.attachment_store.get(attachment_id, conversation_id="conv_legacy_owner", workspace_root="") == "original uploaded file"
    assert session.artifact_store.get(artifact_id, conversation_id="conv_legacy_owner", workspace_root=directory) == "original output"
    assert session.artifact_store.get(artifact_id, conversation_id="conv_legacy_owner", workspace_root="") == "original output"
    assert repository.get_conversation("conv_legacy_owner").workspace_root == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("has_recorded_cwd", [False, True])
async def test_real_rest_admission_uses_fixed_cwd_and_preserves_legacy_resource_grants(tmp_path, monkeypatch, has_recorded_cwd):
    from backend.agent.query_engine import QueryEngine
    from backend.agent.runtime import AgentRuntime
    from backend.config import AppConfig, AgentSettings, LLMSettings, TokenBudget
    from backend.services import chat_api_service as rest
    from backend.tests.test_core_model_audit_closure import Model
    from backend.tools.registry import ToolRegistry

    root = tmp_path / "actual-rest-operation"
    root.mkdir()
    monkeypatch.chdir(root)
    repository = ConversationRepository(tmp_path / "conversations")
    snapshot = {"execution_cwd": str(root)} if has_recorded_cwd else {}
    conversation = repository.create_conversation(context_snapshot=snapshot)
    attachments = AttachmentStore(tmp_path / "attachments")
    attachments.save(artifact_id="rest-original", content="original upload", metadata={"conversation_id": conversation.id, "workspace_root": ""})
    artifacts = ArtifactStore(storage_dir=tmp_path / "artifacts")
    artifact_id = artifacts.save("original result", source="rest-original", type="text", conversation_id=conversation.id, workspace_root="")
    monkeypatch.setattr("backend.conversations.repository.ConversationRepository", lambda: repository)
    monkeypatch.setattr("backend.services.tool_registry_factory.get_attachment_store", lambda: attachments)
    monkeypatch.setattr(rest, "ArtifactStore", lambda: artifacts)
    config = AppConfig(llm=LLMSettings(api_key="offline", model="audit-model"),
        agent=AgentSettings(max_iterations=5, code_mode_only=False, stream_max_attempts=0), token_budget=TokenBudget(total=100000, response_reserve=1024))
    monkeypatch.setattr(rest, "load_config", lambda **kwargs: config)
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", enable_lease_heartbeat=False)
    monkeypatch.setattr(rest, "default_runtime", lambda: runtime)
    llm = Model("two_final")
    submitted = []

    class CaptureEngine(QueryEngine):
        async def submit(self, submission):
            submitted.append(submission)
            async for event in super().submit(submission):
                yield event

    bootstrap = SimpleNamespace(create_tool_registry=lambda *args, **kwargs: ToolRegistry(),
        create_permission_checker=lambda **kwargs: PermissionChecker(PermissionSettings(), workspace_root=kwargs["workspace_root"]),
        create_llm=lambda **kwargs: llm, mcp_manager=None)
    try:
        result = await rest.run_rest_chat(message="Continue the original task", max_iterations=None, bootstrap=bootstrap,
            query_engine=CaptureEngine(), conversation_id=conversation.id, permission_mode="bypass")
        assert result["status"] == "completed" and result["reply"] == "First.Second."
        assert submitted[0].state.workspace_root == root and submitted[0].runtime.workspace_root == root
        fixed = repository.get_conversation(conversation.id)
        assert fixed.execution_cwd == str(root) and fixed.workspace_root == ""
        assert attachments.get("rest-original", conversation_id=conversation.id, workspace_root=root) == "original upload"
        assert attachments.get("rest-original", conversation_id=conversation.id, workspace_root="") == "original upload"
        assert artifacts.get(artifact_id, conversation_id=conversation.id, workspace_root=root) == "original result"
        assert artifacts.get(artifact_id, conversation_id=conversation.id, workspace_root="") == "original result"
    finally:
        runtime.close(release_lease=True)


def test_unrecorded_projectless_read_only_navigation_does_not_guess_or_bind_cwd(tmp_path, monkeypatch):
    directory = tmp_path / "actual-operation"
    directory.mkdir()
    repository = ConversationRepository(tmp_path / "conversations")
    conversation = repository.create_conversation()
    session = owner_session(repository, conversation.id, tmp_path)
    monkeypatch.chdir(directory)
    assert conversation.execution_cwd == ""
    assert session.session_lifecycle.current_workspace_root() is None
    assert session.session_lifecycle.workspace_root_for_conversation(conversation) is None
    assert repository.get_conversation(conversation.id).execution_cwd == ""
    bound = session.session_lifecycle.bind_execution_for_operation(conversation)
    assert bound.execution_cwd == str(directory)
    assert bound.workspace_root == ""
    assert session.session_lifecycle.current_workspace_root() == directory


@pytest.mark.asyncio
async def test_real_create_dispatch_projects_fixed_cwd_without_project_membership(tmp_path, trusted_ledger, monkeypatch):
    from backend.agent.message import UserCommand
    from backend.tests.test_ws_cold_connection import _connection, _release_session
    from backend.ws.manager import WebSocketManager
    from backend.workspace.recent_projects import RecentProjectStore
    from backend.services.workspace_service import list_workspace_recent_payload

    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")
    recent_path = tmp_path / "desktop-state" / "recent_projects.json"
    monkeypatch.setattr("backend.workspace.recent_projects.DEFAULT_STORE_PATH", recent_path)
    existing_project = tmp_path / "existing-real-project"
    existing_project.mkdir()
    RecentProjectStore().add(str(existing_project), existing_project.name)
    existing_recent = recent_path.read_bytes()
    connection = _connection(tmp_path, session_id="projectless-create-cwd")
    manager = WebSocketManager()
    session, _ = await manager.connect(**connection)
    monkeypatch.setattr(session, "refresh_llm_selection", lambda **_: None)
    monkeypatch.setattr(session.session_lifecycle, "schedule_runtime_capabilities", lambda **_: None)
    try:
        await session.command_dispatcher._dispatch_client_command(UserCommand("conversation.create", {
            "projectless_base": str(tmp_path / "tasks"), "initial_prompt": "create final report", "client_command_id": "create-projectless",
        }), session.connection_generation)
        await session.event_outbox.drain_delivery()
        switched = next(event for event in connection["websocket"].sent if event.get("type") == "conversation.switched")
        created = session.conversation_repo.get_conversation(switched["conversation_id"])
        assert Path(created.execution_cwd).is_dir()
        assert switched["conversation"]["execution_cwd"] == created.execution_cwd
        assert switched["session"]["workspace_root"] == created.execution_cwd
        assert switched["session"]["workspace_scope"] == "computer"
        assert created.workspace_root == ""
        assert session.session_lifecycle.workspace_context.root_path == Path(created.execution_cwd)
        scope = resolve_command_scope(session, {"conversation_id": created.id}, require_workspace=True)
        assert scope.workspace_root == created.execution_cwd
        context_task = session.session_lifecycle.workspace_context_task
        if context_task is not None:
            await context_task
        assert session.session_lifecycle.workspace_context.metadata is not None
        assert recent_path.read_bytes() == existing_recent
        assert [project["path"] for project in list_workspace_recent_payload()["projects"]] == [str(existing_project)]
        assert not any(event.get("type") == "workspace.imported" for event in connection["websocket"].sent)

        # Opening this very same folder explicitly as a project is legitimate,
        # even though its physical context is already mounted by the chat.
        await session.command_dispatcher._dispatch_client_command(UserCommand("workspace.import", {
            "path": created.execution_cwd, "client_command_id": "explicit-project-at-taskdir",
        }), session.connection_generation)
        await session.event_outbox.drain_delivery()
        explicit = session.conversation_repo.get_conversation(session.active_conversation_id)
        assert explicit.id != created.id and explicit.workspace_root == created.execution_cwd
        recent = [event for event in connection["websocket"].sent if event.get("type") == "workspace.recent.list"][-1]
        assert [project["path"] for project in recent["projects"]] == [str(existing_project), created.execution_cwd]
        assert session.conversation_repo.get_conversation(created.id).workspace_root == ""
        await session.command_dispatcher._dispatch_client_command(UserCommand("conversation.switch", {
            "conversation_id": created.id, "client_command_id": "return-projectless",
        }), session.connection_generation)
        assert [project["path"] for project in list_workspace_recent_payload()["projects"]] == [str(existing_project), created.execution_cwd]

        project = tmp_path / "project-with-subcwd"
        subcwd = project / "src"
        subcwd.mkdir(parents=True)
        trust.trust_created_task_directory(project)
        await session.command_dispatcher._dispatch_client_command(UserCommand("conversation.create", {
            "workspace_root": str(project), "execution_cwd": str(subcwd), "client_command_id": "project-subcwd",
        }), session.connection_generation)
        actual_project = session.conversation_repo.get_conversation(session.active_conversation_id)
        assert actual_project.workspace_root == str(project) and actual_project.execution_cwd == str(subcwd)
        projects = list_workspace_recent_payload()["projects"]
        assert [entry["path"] for entry in projects] == [str(existing_project), created.execution_cwd, str(project)]
        assert projects[-1]["name"] == project.name
    finally:
        await _release_session(session)


def test_real_pipe_selected_shell_input_keeps_cwd_variables_and_restart_shell(tmp_path, trusted_ledger):
    async def verify():
        repository = ConversationRepository(tmp_path / "conversations")
        conversation = repository.create_conversation(projectless_base=str(tmp_path / "tasks"), initial_prompt="shell chain")
        root = Path(conversation.execution_cwd)
        (root / "nested").mkdir()
        session = owner_session(repository, conversation.id, tmp_path)
        session.terminal_manager = TerminalSessionManager()
        session.active_terminal_session_id = None
        payloads = []
        results = []
        output = []

        async def on_output(sid, chunk, *args):
            output.append(chunk)

        async def on_exit(*args):
            return None

        async def send_payload(payload, **kwargs):
            payloads.append(payload)

        async def result(command, message, **kwargs):
            results.append({"command": command, "message": message, **kwargs})

        session.session_lifecycle.on_terminal_output = on_output
        session.session_lifecycle.on_terminal_exit = on_exit
        session.resolve_workspace_cwd = lambda cwd=None: Path(cwd or root).resolve()
        session.send_payload = send_payload
        session.send_event = lambda event: send_payload(event.to_ws_message())
        session.emit_command_result = result
        selected = "cmd" if os.name == "nt" else "system"

        async def wait_output(marker):
            while marker not in "".join(output):
                await asyncio.sleep(0.01)

        try:
            await handle_terminal_create(session, {"conversation_id": conversation.id, "shell": selected})
            first_id = session.active_terminal_session_id
            first = session.terminal_manager.get_session(first_id)
            assert first.is_alive and first.terminal_mode == "pipe"
            assert first.shell.startswith("cmd.exe") if os.name == "nt" else first.shell.startswith(os.environ.get("SHELL", "/bin/bash"))
            assert payloads[-1]["cwd"] == str(root)
            commands = "set MINICODE_SHELL_PROOF=actual-state\rcd nested\recho %MINICODE_SHELL_PROOF%\recho %CD%\recho shell-state-done\r" if os.name == "nt" else "MINICODE_SHELL_PROOF=actual-state\ncd nested\necho $MINICODE_SHELL_PROOF\npwd\necho shell-state-done\n"
            await handle_terminal_input(session, {"session_id": first_id, "data": commands})
            await asyncio.wait_for(wait_output("shell-state-done"), timeout=10)
            assert "actual-state" in "".join(output)
            assert str(root / "nested") in "".join(output)
            await handle_terminal_restart(session, {"session_id": first_id})
            restarted = session.terminal_manager.get_session(session.active_terminal_session_id)
            assert restarted.session_id != first_id and restarted.shell == first.shell
            assert restarted._initial_cwd == str(root)
            assert not first.is_alive
            assert repository.get_conversation(conversation.id).workspace_root == ""
        finally:
            await session.terminal_manager.destroy_sessions_for_conversation(conversation.id)

    asyncio.run(verify())
