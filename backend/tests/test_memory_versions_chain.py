from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.state import AgentState
from backend.config import AppConfig, LLMSettings
from backend.conversations.repository import ConversationRepository
from backend.feature_flags import FeatureFlags
from backend.llm.base import LLMAdapter, LLMMessage, StreamEvent, StreamEventType, ToolCallEvent
from backend.memory import generation
from backend.memory.file_memory import FileMemory
from backend.memory.generation import MemoryGenerationCoordinator, MemoryGenerationError
from backend.memory.job_store import MEMORY_DB_NAME, MemoryJobStore
from backend.memory.manager import MemoryManager
from backend.memory.prompts import valid_v2_summary
from backend.memory.rollout_input import serialize_tiered_input
from backend.memory.settings import MemorySettings, MemoryVersion, memory_settings_from_config
from backend.memory.text_utils import split_evidence_chunks
from backend.services.memory_read_service import memory_status, thread_memory_read
from backend.tools.memory_tools import MemoryReadTool
from backend.ws.execution_context import ConversationExecutionRegistry


SUMMARY = "v1\n## User Profile\nVerified task context.\n## User preferences\nNo unsupported preference.\n## General Tips\n\n## What's in Memory\nA retained task.\n"


@pytest.fixture
def memory_root(monkeypatch):
    with TemporaryDirectory(prefix="mc-memory-v2-") as directory:
        root = Path(directory) / "data"
        root.mkdir()
        monkeypatch.setattr("backend.memory.file_memory.DATA_ROOT", root)
        monkeypatch.setattr("backend.memory.file_memory.MEMORY_DIR", root / "memory")
        yield root


@pytest.mark.parametrize("enabled,existing,values,version", [
    (False, False, {}, "v1"), (True, False, {}, "v2"), (True, True, {}, "v1"),
    (True, False, {"version": "v1"}, "v1"), (True, False, {"dual_write": False}, "v1"),
    (True, True, {"version": "v2", "dual_write": False}, "v2"),
])
def test_version_selection_preserves_existing_profiles_and_explicit_choices(memory_root, enabled, existing, values, version):
    if existing:
        FileMemory()
    settings = memory_settings_from_config({"memories": values}, enabled=enabled)
    assert settings.version == version
    assert FileMemory.version_root(MemoryVersion.V1).exists() is existing
    assert not FileMemory.version_root(MemoryVersion.V2).exists()
    with pytest.raises(ValueError):
        memory_settings_from_config({"memories": {"version": ""}}, enabled=enabled)


def test_v2_evidence_prioritizes_human_answers_without_promoting_agent_context():
    question = {"questions": [{"id": "style", "question": "Which output is needed?"}]}
    transcript = [
        {"role": "user", "content": "Keep the older task and its approval boundary."},
        {"role": "assistant", "blocks": [
            {"type": "tool_call", "record": {"id": "question", "name": "request_user_input", "args": question,
                "status": "success", "summary": json.dumps({"answers": {"style": {"answers": ["Editable PowerPoint, not an image"]}}})}},
            {"type": "process", "source": "model_commentary", "content": "Assistant tentative idea " * 2000},
            {"type": "text", "source": "model_final", "content": "Completed original task."},
            {"type": "tool_call", "record": {"id": "log", "name": "run_command", "status": "success", "summary": "REPEATED_LOG " * 5000}},
        ]},
        {"role": "user", "content": "Message Type: MESSAGE\nTask name: worker\nSender: worker\nPayload:\nAssistant suggestion, not a user preference."},
        {"role": "user", "content": "# AGENTS.md instructions\n<INSTRUCTIONS>EXCLUDED_GUIDE</INSTRUCTIONS>"},
        {"role": "user", "content": "New objective. Keep both task scopes."},
    ]
    rendered = serialize_tiered_input(transcript, max_bytes=2200)
    assert len(rendered.encode("utf-8")) <= 2200
    for retained in ["older task", "Which output is needed?", "Editable PowerPoint", "Completed original task", "New objective"]:
        assert retained in rendered
    assert "EXCLUDED_GUIDE" not in rendered
    assert "[other agent]" in rendered
    assert rendered.index("older task") < rendered.index("Human reply:") < rendered.index("New objective")
    assert "[... response items omitted ...]" in rendered


def test_memory_chunks_preserve_unicode_and_summary_contract():
    text = "中文🙂" * 4000
    chunks = split_evidence_chunks(text)
    assert "".join(chunks) == text
    assert all(len(chunk.encode("utf-8")) <= 8900 for chunk in chunks)
    assert valid_v2_summary(SUMMARY)
    assert not valid_v2_summary(SUMMARY.replace("## User preferences", "## Preferences"))
    assert not valid_v2_summary(SUMMARY + "中" * 4000)


def test_tiered_evidence_retains_actual_process_and_delegate_projection_shapes():
    transcript = [{"role": "assistant", "blocks": [
        {"type": "process", "source": "model_preamble", "content": "I'll inspect the real source."},
        {"type": "progress", "subagentId": "worker", "subagentName": "Reviewer", "subagentStatus": "completed",
            "detail": "The delegate found an ownership mismatch."},
        {"type": "text", "source": "model_final", "content": "The result was verified."},
    ]}]
    text = serialize_tiered_input(transcript, max_bytes=5000)
    assert '[assistant commentary]' in text and "inspect the real source" in text
    assert '[other agent]' in text and "delegate found" in text
    assert '[assistant final]' in text and "result was verified" in text


class MemoryModel(LLMAdapter):
    model = "memory-fixture"
    provider_name = "custom"

    def __init__(self):
        self.extractions = []
        self.consolidations = []

    async def side_query(self, messages, *, options):
        self.extractions.append((messages, options))
        result = {"rollout_summary": "Extracted source-supported task 中文", "rollout_slug": "verified-task"}
        if "raw_memory" in options.output_schema["properties"]:
            result["raw_memory"] = "V1 durable fact"
        return json.dumps(result)

    async def stream_chat(self, messages, tools=None, metadata=None):
        self.consolidations.append(messages)
        if not any(message.role == "tool" and message.tool_call_id == "memory-write" for message in messages):
            assert any(tool["function"]["name"] == "write_file" for tool in tools)
            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=[ToolCallEvent(
                id="memory-write", name="write_file", arguments={"file_path": "memory_summary.md", "content": SUMMARY})])
        else:
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Published the supplied memory summary.", phase="final_answer")
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

    async def simple_chat(self, messages, **kwargs):
        raise AssertionError("Extraction must send its actual output schema")


@pytest.mark.asyncio
async def test_v2_runs_real_consolidation_tool_then_reads_same_namespace(memory_root, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = ConversationRepository(tmp_path / "conversations")
    source = repository.create_conversation(workspace_root=str(workspace), transcript=[
        {"id": "user", "role": "user", "content": "The explicitly requested task."},
        {"id": "assistant", "role": "assistant", "content": "The verified result."},
    ])
    model = MemoryModel()
    owner = MemoryGenerationCoordinator(repository=repository, llm=model, workspace_root=workspace,
        settings=MemorySettings(version=MemoryVersion.V2), now=lambda: time.time() + 7 * 3600)
    await owner.run_startup(current_conversation_id="current")
    assert len(model.extractions) == 1
    assert len(model.consolidations) >= 2
    messages, options = model.extractions[0]
    assert set(options.output_schema["properties"]) == {"rollout_summary", "rollout_slug"}
    assert all(message.content_kind == "memories.extraction_evidence" for message in messages[1:])
    assert owner.file_memory.read_summary() == SUMMARY
    assert not (owner.memory_root / "MEMORY.md").exists()
    assert not (owner.memory_root / "raw_memories.md").exists()
    outputs = owner.store.list_stage1_outputs(limit=None, max_unused_days=30)
    assert [(item.thread_id, item.raw_memory, item.selected_for_phase2) for item in outputs] == [(source.id, "", True)]
    assert owner.store.max_consolidated_thread_count() == 1
    assert not FileMemory.version_root(MemoryVersion.V1).exists()
    builder = ContextBuilder(memory_manager=MemoryManager(owner.file_memory), workspace_root=workspace)
    prompt = builder._render_prompt_messages(AgentState(user_message="continue"), workspace)
    parts = [message for message in prompt if message.content_kind == "memories.instructions"]
    assert parts and all(message.role == "developer" for message in parts)
    assert "A retained task" in "".join(message.content for message in parts)
    result = await MemoryReadTool(owner.file_memory).execute({"path": "memory_summary.md"}, SimpleNamespace(workspace_root=workspace))
    assert not result.is_error and "A retained task" in result.content


@pytest.mark.asyncio
async def test_dual_write_isolates_claims_and_cutover_never_reads_v1(memory_root, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = ConversationRepository(tmp_path / "conversations")
    source = repository.create_conversation(workspace_root=str(workspace), transcript=[{"id": "user", "role": "user", "content": "Retain this verified task"}])
    monkeypatch.setattr(MemoryGenerationCoordinator, "_now", lambda self: int(time.time()) + 7 * 3600)
    async def consolidate(*, memory_root, **kwargs):
        if (memory_root / "raw_memories.md").exists():
            (memory_root / "MEMORY.md").write_text("V1 handbook", encoding="utf-8")
            summary = "v1\nV1 ONLY"
        else:
            summary = SUMMARY
        (memory_root / "memory_summary.md").write_text(summary, encoding="utf-8")
    monkeypatch.setattr(generation, "run_memory_consolidation_agent", consolidate)
    model = MemoryModel()
    task = generation.schedule_memory_startup(repository=repository, llm=model, workspace_root=workspace,
        current_conversation_id="current", settings=MemorySettings(dual_write=True))
    await task
    assert len(model.extractions) == 2
    memories = {version: FileMemory.for_workspace(workspace, version=version) for version in MemoryVersion}
    assert memories[MemoryVersion.V1].read_summary() == "v1\nV1 ONLY"
    assert memories[MemoryVersion.V2].read_summary() == SUMMARY
    for version, memory in memories.items():
        store = MemoryJobStore(memory.memory_dir / MEMORY_DB_NAME, reset_lock=memory.reset_lock, version=version)
        assert [item.thread_id for item in store.list_stage1_outputs(limit=None, max_unused_days=30)] == [source.id]
    (memories[MemoryVersion.V2].memory_dir / "memory_summary.md").unlink()
    assert memories[MemoryVersion.V2].get_context() == ""
    assert "V1 ONLY" in memories[MemoryVersion.V1].get_context()
    await generation.schedule_memory_forgetting(repository=repository, llm=None, workspace_root=workspace, conversation_id=source.id)
    for version, memory in memories.items():
        assert MemoryJobStore(memory.memory_dir / MEMORY_DB_NAME, reset_lock=memory.reset_lock, version=version).list_stage1_outputs(limit=None, max_unused_days=30) == []
    manager = MemoryManager(FileMemory(version=MemoryVersion.V2))
    manager.reset()
    assert not any(memory.memory_dir.exists() for memory in memories.values())


@pytest.mark.asyncio
async def test_thread_read_and_readiness_use_captured_version(memory_root, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = ConversationRepository(tmp_path / "conversations")
    conversation = repository.create_conversation(workspace_root=str(workspace))
    registry = ConversationExecutionRegistry()
    session = SimpleNamespace(conversation_repo=repository, active_conversation_id=conversation.id,
        ws_manager=SimpleNamespace(execution_contexts=registry),
        session_lifecycle=SimpleNamespace(workspace_root_for_conversation=lambda record: workspace))
    config = AppConfig(llm=LLMSettings(api_key="fixture"), feature_flags=FeatureFlags({"memories": True}), memories=MemorySettings(version=MemoryVersion.V2))
    monkeypatch.setattr("backend.services.memory_read_service.load_config", lambda **kwargs: config)
    v2 = FileMemory.for_workspace(workspace, version=MemoryVersion.V2)
    (v2.memory_dir / "memory_summary.md").write_text(SUMMARY, encoding="utf-8")
    assert (await thread_memory_read(session, conversation.id))["summary"] == SUMMARY
    config = replace(config, memories=MemorySettings(version=MemoryVersion.V1))
    assert (await thread_memory_read(session, conversation.id))["summary"] == SUMMARY
    config = replace(config, memories=replace(config.memories, use_memories=False))
    assert await thread_memory_read(session, conversation.id) == {"summary": None}
    assert await memory_status(session) == {"v2ConsolidatedThreads": 0, "v2Ready": True}
    FileMemory.for_workspace(workspace)
    assert await memory_status(session) == {"v2ConsolidatedThreads": 0, "v2Ready": False}
    with pytest.raises(ValueError, match="between"):
        await memory_status(session, minimum=0)


@pytest.mark.asyncio
async def test_v2_output_is_strict_and_bounded_after_redaction(memory_root, tmp_path):
    model = MemoryModel()
    repository = ConversationRepository(tmp_path / "conversations")
    record = repository.create_conversation(workspace_root=str(tmp_path))
    owner = MemoryGenerationCoordinator(repository=repository, llm=model, workspace_root=tmp_path,
        settings=MemorySettings(version=MemoryVersion.V2))
    model.side_query = AsyncMock(return_value=json.dumps({"raw_memory": "not a V2 field", "rollout_summary": "summary", "rollout_slug": "task"}))
    with pytest.raises(MemoryGenerationError, match="exactly"):
        await owner._extract_phase1(record, "source", startup_source=None)
    model.side_query = AsyncMock(return_value=json.dumps({"rollout_summary": "中文🙂" * 4000, "rollout_slug": "task"}))
    result = await owner._extract_phase1(record, "source", startup_source=None)
    assert result.raw_memory == ""
    assert len(result.rollout_summary.encode("utf-8")) <= 9000
    assert result.rollout_summary.startswith("中文") and result.rollout_summary.endswith("🙂")


@pytest.mark.asyncio
async def test_ws_memory_queries_return_exact_thread_and_version_readiness(memory_root, tmp_path, monkeypatch):
    from backend.agent.message import UserCommand
    from backend.tests.test_user_input_presentation_chain import session_for
    from backend.ws.handlers import HANDLERS

    session, conversation, _ = session_for(tmp_path, monkeypatch)
    config = replace(session.config, feature_flags=FeatureFlags({"memories": True}),
        memories=MemorySettings(version=MemoryVersion.V2))
    monkeypatch.setattr("backend.services.memory_read_service.load_config", lambda **kwargs: config)
    workspace = session.session_lifecycle.workspace_root_for_conversation(conversation)
    memory = FileMemory.for_workspace(workspace, version=MemoryVersion.V2)
    (memory.memory_dir / "memory_summary.md").write_text(SUMMARY, encoding="utf-8")
    store = MemoryJobStore(memory.memory_dir / MEMORY_DB_NAME, version=MemoryVersion.V2, reset_lock=memory.reset_lock)
    claim = store.claim_stage1(thread_id=conversation.id, source_revision=conversation.content_revision,
        worker_id="version-test", lease_seconds=60, retry_limit=3, max_running_jobs=8)
    assert store.complete_stage1(claim, raw_memory="", rollout_summary="retained", rollout_slug="retained", source_updated_at=int(time.time()))
    outputs = store.list_stage1_outputs(limit=None, max_unused_days=30)
    consolidation = store.claim_phase2(worker_id="version-test", lease_seconds=60, retry_limit=3, success_cooldown_seconds=0)
    assert store.complete_phase2(consolidation, outputs)
    FileMemory.for_workspace(workspace)
    try:
        for command in [UserCommand("thread/memory/read", {"threadId": conversation.id}),
                        UserCommand("memory/status", {"minConsolidatedThreads": 1})]:
            await session.command_dispatcher._dispatch_client_command(command, session.connection_generation)
        replies = [payload for payload in session.ws.messages if payload.get("type") == "command.result"]
        read = next(payload for payload in replies if payload["command"] == "thread/memory/read")
        assert read["data"]["summary"] == SUMMARY and read["conversation_id"] == conversation.id
        status = next(payload for payload in replies if payload["command"] == "memory/status")
        assert status["data"]["v2Ready"] and status["data"]["v2ConsolidatedThreads"] == 1
        assert "memory/status" in HANDLERS
    finally:
        await session.session_lifecycle.shutdown(reason="versioned memory queries")


@pytest.mark.asyncio
async def test_dual_write_retains_provider_owner_until_both_workers_settle(memory_root, tmp_path, monkeypatch):
    first_failed, second_started, release_second = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def work(owner, **kwargs):
        if owner.version is MemoryVersion.V1:
            first_failed.set()
            raise OSError("first version storage failure")
        second_started.set()
        await release_second.wait()
    monkeypatch.setattr(MemoryGenerationCoordinator, "run_startup", work)
    task = generation.schedule_memory_startup(repository=object(), llm=object(), workspace_root=tmp_path,
        current_conversation_id="current", settings=MemorySettings(dual_write=True))
    try:
        await first_failed.wait()
        await second_started.wait()
        await asyncio.sleep(0)
        assert not task.done(), "The provider lease belongs to both version workers"
        release_second.set()
        with pytest.raises(ExceptionGroup, match="Memory version workers failed") as failed:
            await task
        assert isinstance(failed.value.exceptions[0], OSError)
    finally:
        release_second.set()
        await asyncio.gather(task, return_exceptions=True)
