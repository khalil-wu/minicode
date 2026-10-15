"""Release audit regressions using isolated stores and captured provider requests."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from backend.agent.context import CompactionNoopError, ContextBuilder
from backend.agent.provider_protocol import provider_raw_for_projection
from backend.agent.provider_stream_error_event import provider_error_details
from backend.agent.provider_stream_settlement import settle_provider_stream
from backend.sandbox.policy import ResolvedSandboxPolicy, SandboxEnforcement, NetworkSandboxPolicy
from backend.agent.skill_activation import activate_turn_skills
from backend.agent.state import AgentState
from backend.agent.stream_sanitizer import ThinkingStreamSanitizer
from backend.artifact.store import ArtifactStore
from backend.attachments.store import AttachmentStore
from backend.config import TokenBudget
from backend.conversations.import_export import import_conversation_tree
from backend.conversations.repository import ConversationRepository
from backend.llm.anthropic_adapter import AnthropicAdapter
from backend.llm.base import LLMMessage, LLMSideCallContext, SideQueryOptions, StreamEvent, StreamEventType, UsageInfo
from backend.llm.errors import classify_llm_error
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.services.context_budget import manage_context_budget
from backend.services.conversation_resources_service import conversation_resource_page
from backend.skills.loader import SkillLoader
from backend.skills.manager import SkillManager
from backend.tools.agent_artifact_tools import ReadArtifactTool


@pytest.mark.parametrize("requested", [None, 64, 1])
def test_anthropic_preserves_real_output_budget_when_estimate_exhausts_window(requested):
    adapter = AnthropicAdapter("fixture", model="claude-sonnet-4-5", context_window=50, max_tokens=8000, thinking_budget=1024)
    captured = []
    async def upstream(payload, **kwargs):
        captured.append(payload)
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="end_turn")
    adapter._stream_messages = upstream
    async def run():
        options = SideQueryOptions(operation="owned-side", max_tokens=requested, disable_reasoning=True) if requested is not None else None
        context = LLMSideCallContext(options=options, record={}) if options else None
        async for _ in adapter._stream_chat_with_context([LLMMessage(role="user", content="long input " * 400)], context=context):
            pass
    asyncio.run(run())
    assert captured[0]["max_tokens"] == (8000 if requested is None else requested)
    if requested is None:
        assert captured[0]["thinking"] == {"type": "enabled", "budget_tokens": 1024}


@pytest.mark.parametrize("code", ["usage_limit_reached", "workspace_owner_usage_limit_reached", "workspace_member_usage_limit_reached"])
def test_usage_exhaustion_stays_terminal_through_raw_and_stream_wire(code):
    payload = json.dumps({"error": {"code": code, "type": "rate_limit_error", "message": "Limit reached"}})
    assert classify_llm_error(payload).fatal and not classify_llm_error(payload).retryable
    event = StreamEvent(type=StreamEventType.ERROR, content="Limit reached", raw={
        "status_code": 429, "error_type": "api", "provider_error_type": "rate_limit", "provider_error_code": code,
    })
    _, classification, details = provider_error_details(event)
    assert classification.fatal and not classification.retryable
    assert classification.provider_error_type == "billing" and details["provider_error_code"] == code
    failure = RuntimeError("Limit reached")
    failure.status_code = 429
    failure.provider_error_code = code
    assert classify_llm_error(failure).fatal and not classify_llm_error(failure).retryable


def test_transient_429_retains_retry_semantics():
    event = StreamEvent(type=StreamEventType.ERROR, content="Please retry later", raw={"status_code": 429, "provider_error_type": "rate_limit"})
    _, classification, _ = provider_error_details(event)
    assert classification.retryable and not classification.fatal


def test_compaction_noop_does_not_poison_across_turn_failure_counter():
    class Context:
        consecutive_autocompact_failures = 0
        hook_manager = None
        calls = 0
        def needs_compaction(self, *args, **kwargs): return True
        def context_ledger(self): return {"estimated_tokens": 900, "entries": []}
        async def compact(self, **kwargs):
            self.calls += 1
            raise CompactionNoopError()
        def record_autocompact_failure(self): self.consecutive_autocompact_failures += 1
    ctx = Context()
    async def run():
        for _ in range(4):
            state = AgentState(user_message="continue")
            events = [event async for event in manage_context_budget(ctx, state, TokenBudget(total=1000), [])]
            assert not events and state.stopped_reason is None
    asyncio.run(run())
    assert ctx.calls == 4 and ctx.consecutive_autocompact_failures == 0


@pytest.mark.parametrize("change", ["renamed", "disabled", "deleted"])
def test_selected_skill_catalog_change_preserves_turn_and_uses_exact_path(tmp_path, change):
    skill_root = tmp_path / "skills"
    path = skill_root / "workflow" / "SKILL.md"
    path.parent.mkdir(parents=True)
    def write(name="old", invocable=True):
        path.write_text(f"---\nname: {name}\ndescription: Owned workflow\nuser-invocable: {str(invocable).lower()}\n---\nOWNED_SKILL_CONTENT\n", encoding="utf-8")
    write()
    loader = SkillLoader()
    loader._search_dirs = lambda: [("workspace", skill_root)]
    manager = SkillManager(loader)
    manager.discover()
    if change == "deleted": path.unlink()
    else: write("current" if change == "renamed" else "old", change != "disabled")
    manager = manager.snapshot()
    state = AgentState(user_message="Continue the task")
    state.prompt_context["selected_skills"] = [{"name": "old", "path": str(path)}]
    async def run():
        notices = [event async for event in activate_turn_skills(manager, state.user_message, state, permission_context=PermissionContext(), permission_profile=ResolvedSandboxPolicy(SandboxEnforcement.MANAGED, NetworkSandboxPolicy.RESTRICTED))]
        builder = ContextBuilder(skill_manager=manager, token_budget=TokenBudget(active_skills=1000))
        await builder.start_turn(state.user_message, state)
        messages = await builder.build(state)
        return notices, messages
    notices, messages = asyncio.run(run())
    injections = [message.content for message in messages if "<skill>" in message.content]
    if change == "renamed":
        assert state.active_skills == ["current"]
        assert len(injections) == 1 and "<name>current</name>" in injections[0]
        assert "OWNED_SKILL_CONTENT" in injections[0]
    else:
        assert not state.active_skills and not injections
        assert any(event.type == "system_notice" for event in notices)
    assert state.stopped_reason is None


def test_memory_citation_survives_hidden_text_settlement_and_cold_transcript(tmp_path):
    sanitizer = ThinkingStreamSanitizer()
    markup = "<minicode-memory-citation><citation_entries>MEMORY.md:2-4|note=[owned memory]</citation_entries><rollout_ids>rollout-owned</rollout_ids></minicode-memory-citation>"
    visible = sanitizer.feed("Answer. " + markup) + sanitizer.finish()
    assert "citation_entries" not in visible
    raw = {"finish_reason": "stop"}
    used = []
    async def close(*args, **kwargs): pass
    async def run():
        return [item async for item in settle_provider_stream(
            retry_budget_boundary=None, budget_runtime=SimpleNamespace(), turn_kernel=SimpleNamespace(close_provider_attempt=close),
            provider_attempt=object(), finish_reason="stop", provider_stream_steered=False, rebuild_context_and_retry=False,
            state=AgentState(user_message="continue"), pending_tool_calls=[], provider_raw_done=raw, provider_done=True,
            visible_text_sanitizer=sanitizer, stream_state=SimpleNamespace(finish_reason="stop"), stream_text=SimpleNamespace(sanitize=lambda scrub: None),
            context_builder=SimpleNamespace(record_memory_citation_usage=lambda ids: used.extend(ids)), usage=UsageInfo(), turn_usage=UsageInfo(),
        )]
    asyncio.run(run())
    projected = provider_raw_for_projection(raw)
    assert used == ["rollout-owned"]
    assert projected["memory_citation"]["entries"][0]["line_start"] == 2
    repo = ConversationRepository(tmp_path / "conversations")
    record = repo.create_conversation(transcript=[{"id": "reply", "role": "assistant", "content": visible,
        "blocks": [{"type": "text", "content": visible, "providerRaw": raw}]}])
    cold = ConversationRepository(tmp_path / "conversations").get_conversation(record.id)
    assert cold.transcript[0]["blocks"][0]["providerRaw"]["memory_citation"] == projected["memory_citation"]


def test_memory_citation_projection_does_not_admit_arbitrary_objects_or_fields():
    projected = provider_raw_for_projection({"memory_citation": {
        "entries": [{"path": "MEMORY.md", "line_start": 1, "line_end": 3, "note": "kept", "prompt": "private"},
                    {"path": {"prompt": "private"}, "line_start": 1, "line_end": 3}],
        "rollout_ids": ["rollout", {"password": "private"}], "headers": {"private": "secret"},
    }})
    assert projected["memory_citation"] == {"entries": [{"path": "MEMORY.md", "line_start": 1, "line_end": 3, "note": "kept"}], "rollout_ids": ["rollout"]}


def _resource_context(tmp_path):
    attachments = AttachmentStore(tmp_path / "attachments")
    artifacts = ArtifactStore(storage_dir=tmp_path / "artifacts")
    attachments.save(artifact_id="healthy", content="healthy content", metadata={"conversation_id": "conv_resource_owner", "attachment": {"file_name": "healthy.txt"}})
    attachments.save(artifact_id="broken", content="private foreign content", metadata={"conversation_id": "foreign", "attachment": {"file_name": "private.txt"}})
    broken = tmp_path / "attachments" / "broken.json"
    broken.write_bytes(b"{truncated")
    repo = ConversationRepository(tmp_path / "conversations")
    repo.create_conversation(conversation_id="conv_resource_owner")
    session = SimpleNamespace(conversation_repo=repo, attachment_store=attachments, artifact_store=artifacts,
        session_lifecycle=SimpleNamespace(workspace_root_for_conversation=lambda record: record.workspace_root))
    return attachments, broken, session, SimpleNamespace(get_session=lambda identifier: session)


def test_corrupt_attachment_inventory_keeps_healthy_items_and_reports_incomplete_scope(tmp_path):
    attachments, broken, session, manager = _resource_context(tmp_path)
    index = tmp_path / "attachments" / "index.json"
    index.unlink()
    session.attachment_store = AttachmentStore(tmp_path / "attachments")
    page = conversation_resource_page(session_id="fixture", conversation_id="conv_resource_owner", ws_manager=manager)
    assert [item["artifact_id"] for item in page["items"]] == ["healthy"]
    assert page["read_errors"][0]["error_type"] == "JSONDecodeError"
    assert not page["read_errors"][0]["artifact_id"]
    assert "broken" not in json.dumps(page["read_errors"]) and "private" not in json.dumps(page["read_errors"])
    assert not index.exists() and broken.read_bytes() == b"{truncated"


def test_corrupt_other_attachment_does_not_break_healthy_alias_resolution(tmp_path):
    attachments, broken, _, _ = _resource_context(tmp_path)
    (tmp_path / "attachments" / "index.json").unlink()
    assert attachments.resolve_content("healthy.txt", conversation_id="conv_resource_owner")[1] == "healthy content"
    with pytest.raises(json.JSONDecodeError):
        attachments.resolve_content("missing.txt", conversation_id="conv_resource_owner")
    assert broken.read_bytes() == b"{truncated"


@pytest.mark.parametrize("invalidate_index", [False, True])
def test_partial_attachment_delete_purges_healthy_owned_bytes_before_real_error(tmp_path, invalidate_index):
    attachments, broken, _, _ = _resource_context(tmp_path)
    if invalidate_index: (tmp_path / "attachments" / "index.json").unlink()
    with pytest.raises(json.JSONDecodeError): attachments.delete_for_conversation("conv_resource_owner")
    assert not (tmp_path / "attachments" / "healthy.json").exists()
    assert broken.read_bytes() == b"{truncated"


def test_multiple_attachment_corruptions_report_aggregate_after_healthy_delete(tmp_path):
    attachments, broken, _, _ = _resource_context(tmp_path)
    (tmp_path / "attachments" / "another-broken.json").write_bytes(b"{also truncated")
    with pytest.raises(ExceptionGroup) as failure: attachments.delete_for_conversation("conv_resource_owner")
    assert len(failure.value.exceptions) == 2
    assert not (tmp_path / "attachments" / "healthy.json").exists() and broken.exists()


@pytest.mark.parametrize("route", ["preview", "raw"])
def test_requested_attachment_corruption_is_http_storage_error_not_missing(tmp_path, monkeypatch, route):
    from backend.api import _state, routes_chat
    attachments, _, session, manager = _resource_context(tmp_path)
    (tmp_path / "attachments" / "healthy.json").write_bytes(b"{bad requested record")
    monkeypatch.setattr(_state, "ws_manager", manager)
    monkeypatch.setattr(routes_chat, "_get_attachment_store", lambda: attachments)
    async def run():
        if route == "preview": await routes_chat.preview_attachment(session_id="fixture", conversation_id="conv_resource_owner", artifact_id="healthy")
        else: await routes_chat.raw_attachment(request=SimpleNamespace(headers={}), session_id="fixture", conversation_id="conv_resource_owner", artifact_id="healthy", asset_token=None, download=False)
    with pytest.raises(HTTPException) as failure: asyncio.run(run())
    assert failure.value.status_code == 500 and "JSONDecodeError" in failure.value.detail
    assert (tmp_path / "attachments" / "healthy.json").exists()


def _import_context(tmp_path):
    original_workspace = str(tmp_path / "original")
    target_workspace = str(tmp_path / "target")
    repo = ConversationRepository(tmp_path / "conversations")
    attachments = AttachmentStore(tmp_path / "attachments")
    artifacts = ArtifactStore(storage_dir=tmp_path / "artifacts")
    owner = "conv_original"
    attachments.save(artifact_id="att_visible", content="visible original", metadata={"conversation_id": owner, "workspace_root": original_workspace, "attachment": {"file_name": "visible.txt"}})
    attachments.save(artifact_id="att_hidden", content="unreferenced private original", metadata={"conversation_id": owner, "workspace_root": original_workspace, "attachment": {"file_name": "hidden.txt"}})
    artifact_id = artifacts.save("real tool output", source="fixture", conversation_id=owner, workspace_root=original_workspace)
    source = repo.create_conversation(conversation_id=owner, workspace_root=original_workspace, permission_mode="bypass", transcript=[
        {"id": "input", "role": "user", "content": "Read it", "attachmentRefs": [{"artifactId": "att_visible", "name": "visible.txt", "kind": "document", "mediaType": "text/plain"}]},
        {"id": "answer", "role": "assistant", "content": "Done", "artifacts": [{"artifactId": artifact_id, "kind": "file"}]},
    ])
    return repo, attachments, artifacts, source, artifact_id, target_workspace


def test_import_restores_only_trusted_referenced_resources_in_new_composite_scope(tmp_path):
    repo, attachments, artifacts, source, artifact_id, target = _import_context(tmp_path)
    result = import_conversation_tree(repo, repo.export_conversation_tree(source.id), target, attachment_store=attachments, artifact_store=artifacts)
    owner = result["conversation_id"]
    assert not result["unavailable_artifacts"]
    assert attachments.get("att_visible", conversation_id=owner, workspace_root=target) == "visible original"
    assert artifacts.get(artifact_id, conversation_id=owner, workspace_root=target) == "real tool output"
    assert attachments.get("att_hidden", conversation_id=owner, workspace_root=target) is None
    assert attachments.get("att_visible", conversation_id=owner, workspace_root=source.workspace_root) is None
    assert repo.get_conversation(owner).permission_mode == "confirm" and repo.get_conversation(source.id).permission_mode == "bypass"
    assert attachments.get("att_visible", conversation_id=source.id, workspace_root=source.workspace_root) == "visible original"


@pytest.mark.parametrize("forgery", ["workspace", "unreferenced", "unknown-source"])
def test_external_import_json_cannot_mint_unverified_attachment_grants(tmp_path, forgery):
    repo, attachments, artifacts, source, artifact_id, target = _import_context(tmp_path)
    payload = repo.export_conversation_tree(source.id)
    if forgery == "workspace": payload["conversations"][0]["workspace_root"] = str(tmp_path / "forged-workspace")
    elif forgery == "unknown-source":
        payload["conversations"][0]["id"] = "conv_external"
        payload["selected_conversation_id"] = "conv_external"
    else: payload["conversations"][0]["transcript"][0]["attachmentRefs"].append({"artifactId": "att_hidden", "name": "hidden.txt", "kind": "document"})
    result = import_conversation_tree(repo, payload, target, attachment_store=attachments, artifact_store=artifacts)
    owner = result["conversation_id"]
    forbidden = "att_hidden" if forgery == "unreferenced" else "att_visible"
    assert attachments.get(forbidden, conversation_id=owner, workspace_root=target) is None
    assert any(item["artifact_id"] == forbidden and item["reason"] == "source_unverified" for item in result["unavailable_artifacts"])


def test_import_marks_actual_missing_durable_tool_body(tmp_path):
    repo, attachments, artifacts, source, artifact_id, target = _import_context(tmp_path)
    (tmp_path / "artifacts" / f"{artifact_id}.txt").unlink()
    artifacts = ArtifactStore(storage_dir=tmp_path / "artifacts")
    result = import_conversation_tree(repo, repo.export_conversation_tree(source.id), target, attachment_store=attachments, artifact_store=artifacts)
    assert {item["artifact_id"]: item["reason"] for item in result["unavailable_artifacts"]} == {artifact_id: "original_unavailable"}


@pytest.mark.parametrize("fail_after_commit", [False, True])
def test_failed_import_rolls_back_new_grants_and_histories_without_touching_originals(tmp_path, monkeypatch, fail_after_commit):
    repo, attachments, artifacts, source, artifact_id, target = _import_context(tmp_path)
    child = repo.create_conversation(conversation_id="conv_child_fixture", parent_conversation_id=source.id, workspace_root=source.workspace_root)
    payload = repo.export_conversation_tree(source.id)
    original_save = repo.save_conversation
    saves = []
    def save(record):
        saves.append(record.id)
        if len(saves) == 2:
            if fail_after_commit: original_save(record)
            raise OSError("owned disk-full injection")
        return original_save(record)
    monkeypatch.setattr(repo, "save_conversation", save)
    with pytest.raises(OSError, match="disk-full"):
        import_conversation_tree(repo, payload, target, attachment_store=attachments, artifact_store=artifacts)
    assert {item.id for item in repo.list_conversations()} == {source.id, child.id}
    for owner in saves:
        assert attachments.get("att_visible", conversation_id=owner, workspace_root=target) is None
        assert artifacts.get(artifact_id, conversation_id=owner, workspace_root=target) is None
    assert attachments.get("att_visible", conversation_id=source.id, workspace_root=source.workspace_root) == "visible original"


@pytest.mark.parametrize("window", [{}, {"offset": 2, "limit": 1}])
def test_read_artifact_model_context_contains_each_returned_line_once(tmp_path, window):
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    identifier = store.save("UNIQUE_FIRST\nUNIQUE_SECOND\nUNIQUE_THIRD", source="fixture", conversation_id="conv_resource_owner", workspace_root=tmp_path)
    context = ToolExecutionContext(permission=PermissionContext(), conversation_id="conv_resource_owner", workspace_root=tmp_path)
    result = asyncio.run(ReadArtifactTool(store).execute({"artifact_id": identifier, **window}, context=context))
    model_result = result.to_context_string()
    assert model_result.count("UNIQUE_SECOND") == 1
    assert ("UNIQUE_FIRST" in model_result) == (not window)
    assert result.content_preview is None


@pytest.mark.parametrize("body", ["", " \n", "{truncated"])
def test_empty_or_corrupt_managed_json_cannot_fall_back_to_weaker_source(tmp_path, monkeypatch, body):
    from backend import managed_settings
    from backend.config_layers import ConfigLayerStack
    from backend.config_requirements import ConfigRequirementsError
    path = tmp_path / "managed-settings.json"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setattr(managed_settings, "_load_admin_platform_settings", lambda: managed_settings.ManagedSettingsResult({}))
    fallback = Mock(side_effect=AssertionError("must not select weaker policy"))
    monkeypatch.setattr(managed_settings, "_load_windows_registry_settings", fallback)
    loaded = managed_settings.load_minicode_managed_settings(tmp_path)
    assert loaded.present and loaded.invalid and loaded.source_kind == "managed_files"
    fallback.assert_not_called()
    with pytest.raises(ConfigRequirementsError): ConfigLayerStack((), managed_policy_errors=loaded.validation_errors)
    assert path.read_text(encoding="utf-8") == body


def test_valid_empty_managed_json_remains_present_and_preserves_source_precedence(tmp_path, monkeypatch):
    from backend import managed_settings
    (tmp_path / "managed-settings.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(managed_settings, "_load_admin_platform_settings", lambda: managed_settings.ManagedSettingsResult({}))
    fallback = Mock(side_effect=AssertionError("must not select weaker policy"))
    monkeypatch.setattr(managed_settings, "_load_windows_registry_settings", fallback)
    loaded = managed_settings.load_minicode_managed_settings(tmp_path)
    assert loaded.present and not loaded.invalid and not loaded.settings
    fallback.assert_not_called()


def test_import_uses_persisted_worktree_owner_before_resetting_git_binding(tmp_path):
    repo, attachments, artifacts, source, artifact_id, target = _import_context(tmp_path)
    worktree = str(tmp_path / "owned-worktree")
    source.worktree_path = worktree
    source.execution_cwd = worktree
    source.git_isolated = True
    repo.save_conversation(source)
    attachments.save(artifact_id="att_visible", content="visible worktree original", metadata={
        "conversation_id": source.id, "workspace_root": worktree, "attachment": {"file_name": "visible.txt"},
    })
    artifacts.share_for_conversation(source.id, source.id, worktree, artifact_ids={artifact_id}, source_workspace_root=source.workspace_root)
    result = import_conversation_tree(repo, repo.export_conversation_tree(source.id), target, attachment_store=attachments, artifact_store=artifacts)
    assert not result["unavailable_artifacts"]
    assert attachments.get("att_visible", conversation_id=result["conversation_id"], workspace_root=target) == "visible worktree original"
    imported = repo.get_conversation(result["conversation_id"])
    assert not imported.worktree_path and not imported.git_isolated
    assert imported.execution_cwd == target
