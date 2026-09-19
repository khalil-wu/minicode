from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

from backend.agent.compaction import format_compaction_history
from backend.agent.context import ContextBuilder
from backend.agent.state import AgentState
from backend.attachments.store import AttachmentStore
from backend.config import AgentSettings, AppConfig, LLMSettings
from backend.conversations.models import ConversationRecord
from backend.conversations.repository import ConversationRepository
from backend.llm.base import LLMMessage, ToolCallEvent
from backend.llm.capabilities import ProviderCapabilities
from backend.memory.generation import MemoryGenerationCoordinator
from backend.memory.job_store import MemoryJobStore
from backend.services.llm_config_service import llm_model_updated_payload
from backend.ws.command_handlers import SessionCommandHandlersMixin


def test_compaction_keeps_command_handle_error_tail_and_call_identity():
    messages = [
        LLMMessage(role="assistant", tool_calls=[
            ToolCallEvent(id="verify-1", name="run_command", arguments={"command": "python verify.py"}),
            ToolCallEvent(id="read-1", name="read_file", arguments={"file_path": "source.py"}),
        ]),
        LLMMessage(role="tool", tool_call_id="read-1", content="file content"),
        LLMMessage(role="tool", tool_call_id="verify-1", is_error=True,
                   content="command_id=bg_running\n" + "构建进度\n" * 3000 + "\nFAILED: expected 3, got 2"),
    ]
    text = format_compaction_history(messages)
    assert "command_id=bg_running" in text
    assert text.endswith("FAILED: expected 3, got 2")
    assert '"call_id":"verify-1","name":"run_command","is_error":true' in text
    assert '"call_id":"read-1","name":"read_file","is_error":false' in text
    assert "tokens truncated" in text
    assert "\ufffd" not in text
    assert len(text.encode("utf-8")) < 3000


def test_two_compactions_and_restore_keep_attachment_originals(tmp_path, monkeypatch):
    store = AttachmentStore(tmp_path / "attachments")
    refs = []
    for kind, media_type in [("image", "image/png"), ("document", "application/pdf"), ("file", "text/plain")]:
        ref = {"artifact_id": f"audit-{kind}", "file_name": f"audit-{kind}", "kind": kind, "media_type": media_type}
        store.save(artifact_id=ref["artifact_id"], content="original constraint", native_data="aGVsbG8=",
                   metadata={"conversation_id": "audit", "workspace_root": str(tmp_path), "attachment": ref})
        refs.append(ref)
    llm = SimpleNamespace(capabilities=ProviderCapabilities(vision=True, native_pdf=True, wire_api="responses"))
    builder = ContextBuilder(llm=llm, conversation_id="audit", workspace_root=tmp_path,
                             agent_settings=AgentSettings(compaction_keep_recent_tokens=1))
    builder._attachment_store = store
    summarized = []

    async def summarize(messages, focus="", *, max_tokens=None):
        summarized.append(format_compaction_history(messages))
        # Deliberately omit attachment identifiers from the generated summary.
        return "Continue implementing the user's constraint."

    monkeypatch.setattr(builder, "_summarize_early", summarize)
    builder._history_store.append(LLMMessage(role="user", content="use these originals", attachment_refs=refs))
    for iteration in range(2):
        builder._history_store.append(LLMMessage(role="assistant", content="work completed"))
        builder._history_store.append(LLMMessage(role="user", content=f"continue step {iteration}"))
        asyncio.run(builder.compact())
        assert [ref["artifact_id"] for ref in builder._history[0].attachment_refs] == [ref["artifact_id"] for ref in refs]
    assert all("audit-image" in text and "audit-document" in text and "audit-file" in text for text in summarized)
    restored = ContextBuilder(llm=llm, conversation_id="audit", workspace_root=tmp_path)
    restored._attachment_store = store
    restored.load_snapshot(builder.export_snapshot())
    messages = asyncio.run(restored.build(AgentState(user_message="continue", conversation_id="audit", workspace_root=tmp_path)))
    assert sum(len(message.images) for message in messages) == 1
    assert sum(len(message.documents) for message in messages) == 1
    for ref in refs:
        assert store.get(ref["artifact_id"], conversation_id="audit", workspace_root=str(tmp_path)) == "original constraint"


def test_memory_ignores_metadata_and_snapshot_changes_but_reprocesses_content(tmp_path):
    repo = ConversationRepository(tmp_path / "conversations")
    created = repo.create_conversation(transcript=[{"role": "user", "content": "original request"}])
    jobs = MemoryJobStore(tmp_path / "memory.sqlite3")
    options = dict(thread_id=created.id, worker_id="test", lease_seconds=60, retry_limit=1, max_running_jobs=1)
    source_revision = MemoryGenerationCoordinator._source_revision(created)
    claim = jobs.claim_stage1(source_revision=source_revision, now=1000, **options)
    assert jobs.complete_stage1(claim, raw_memory="fact", rollout_summary="summary", rollout_slug=None,
                                source_updated_at=1000, now=1001)
    for mutate in [
        lambda: repo.rename_conversation(created.id, "display name"),
        lambda: repo.update_model_selection(created.id, provider="openai", model="gpt-5", reasoning_effort="high"),
        lambda: repo.save_context_snapshot(created.id, {"ui_agent_state": {"expanded": True}}),
    ]:
        updated = mutate()
        assert updated.content_revision == source_revision
        assert updated.content_updated_at == created.content_updated_at
        assert jobs.claim_stage1(source_revision=MemoryGenerationCoordinator._source_revision(updated), now=1002, **options) is None
    repo.append_transcript_message(created.id, {"role": "user", "content": "new requirement"})
    restored = ConversationRepository(tmp_path / "conversations").get_conversation(created.id)
    assert restored.content_revision > source_revision
    assert jobs.claim_stage1(source_revision=MemoryGenerationCoordinator._source_revision(restored), now=1003, **options) is not None


def test_model_selection_survives_clone_compaction_and_stale_initialization(tmp_path):
    repo = ConversationRepository(tmp_path)
    created = repo.create_conversation(transcript=[{"role": "user", "content": "keep task settings"}])
    selected = repo.update_model_selection(created.id, provider="openai", model="gpt-5", reasoning_effort="high")
    stale = repo.update_model_selection(created.id, provider="custom", model="old", only_if_unset=True)
    assert stale.model_selection == selected.model_selection
    assert stale.revision == selected.revision
    repo.save_context_snapshot(created.id, {"history": [{"role": "user", "content": "compacted"}]})
    restored = ConversationRepository(tmp_path).get_conversation(created.id)
    clone = repo.clone_conversation(created.id)
    assert restored.model_selection == clone.model_selection == selected.model_selection
    repo.update_model_selection(clone.id, provider="custom", model="other", reasoning_effort="low")
    assert repo.get_conversation(created.id).model_selection == selected.model_selection


def test_legacy_records_seed_content_revision_without_resetting_memory_version():
    record = ConversationRecord.from_dict({"id": "old", "revision": 42, "updated_at": "2026-09-01T00:00:00Z"})
    assert record.content_revision == 42
    assert record.content_updated_at == record.updated_at
    assert record.model_selection == {}


class _SelectionSession(SessionCommandHandlersMixin):
    def __init__(self, repo, active_id, config):
        self.conversation_repo = repo
        self.active_conversation_id = active_id
        self.config = config
        self.provider = "openai"
        self.selected_model = "base"
        self._model_override_active = False
        self._provider_override_active = False
        self._resolve_llm_provider = lambda *_: "openai"
        self._resolve_available_models = lambda *_: ["base", "coding", "vision"]
        self._resolve_models_source = lambda *_: "configured"
        self._model_runtime_for_conversation = lambda *_: None
        self.session_lifecycle = SimpleNamespace(workspace_root_for_conversation=lambda *_: None)
        self.context_builder = SimpleNamespace(bind_llm=lambda _: None, bind_budget=lambda _: None)
        self.run_manager = SimpleNamespace(publish_model_execution=lambda *_: None)

    @property
    def active_conversation(self):
        return self.conversation_repo.get_conversation(self.active_conversation_id)


def test_two_sessions_restore_task_selection_without_changing_captured_adapter(tmp_path, monkeypatch):
    config = AppConfig(llm=LLMSettings(api_key="test", model="base", reasoning_effort="medium"))
    monkeypatch.setattr("backend.config.load_config", lambda **_: config)
    monkeypatch.setattr("backend.ws.agent_runner._config_with_runtime_model_budget", lambda value, **_: value)
    monkeypatch.setattr("backend.ws.agent_runner._get_or_create_session_llm",
                        lambda _, *, config, provider, model, **kwargs: SimpleNamespace(config=config, provider=provider, model=model,
                            supported_reasoning_efforts=lambda: ("off", "low", "medium", "high"), apply_reasoning_policy=lambda _: None))
    repo = ConversationRepository(tmp_path)
    a = repo.create_conversation()
    b = repo.create_conversation()
    first = _SelectionSession(repo, a.id, config)
    second = _SelectionSession(repo, b.id, config)
    asyncio.run(first.set_selected_model("coding", manual_override=True))
    asyncio.run(second.set_selected_model("vision", manual_override=True))
    captured_adapter = first.llm
    repo.update_model_selection(a.id, provider="openai", model="coding", reasoning_effort="high")
    first.active_conversation_id = b.id
    first.refresh_llm_selection()
    assert first.selected_model == first.llm.model == "vision"
    first.active_conversation_id = a.id
    first.refresh_llm_selection()
    assert first.selected_model == "coding"
    assert first.config.llm.reasoning_effort == "high"
    assert captured_adapter.config.llm.reasoning_effort == "medium"
    assert config.llm.reasoning_effort == "medium"
    restarted = _SelectionSession(ConversationRepository(tmp_path), a.id, replace(config))
    restarted.refresh_llm_selection()
    assert restarted.llm.model == "coding"
    assert restarted.config.llm.reasoning_effort == "high"


def test_task_effort_overrides_global_value_in_ui_payload():
    payload = llm_model_updated_payload(provider="openai", selected_model="gpt-5", available_models=["gpt-5"],
                                       workspace_root=None, settings_data={"openai": {"reasoning_effort": "low"}},
                                       configured_reasoning_effort="high")
    assert payload["configured_reasoning_effort"] == "high"
