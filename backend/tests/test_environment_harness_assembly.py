"""Production SDK and ContextBuilder against real local/Source attachments."""
from __future__ import annotations

from contextlib import aclosing
from dataclasses import replace
import os

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.environment_prompt import load_environment_project_instructions
from backend.agent.environment_selection import TurnEnvironmentRequest, TurnEnvironmentSelection, TurnEnvironmentSnapshot
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.config import AgentSettings
from backend.llm.base import LLMAdapter, LLMMessage, StreamEvent, StreamEventType
from backend.sdk import create_session, create_tool_registry
from backend.tests.test_executor_tool_admission import executor_host
from backend.tests.test_executor_tool_admission import batch


class CaptureModel(LLMAdapter):
    def __init__(self):
        self.requests = []

    async def simple_chat(self, messages, **kwargs):
        raise AssertionError("No auxiliary model request expected")

    async def stream_chat(self, messages, tools=None, metadata=None):
        self.requests.append(messages)
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Captured the accepted environment.", phase="final_answer")
        yield StreamEvent(type=StreamEventType.DONE)


@pytest.mark.asyncio
async def test_context_builder_reads_selected_project_docs_and_git_without_controller_workspace(executor_host, tmp_path):
    host = executor_host
    target = tmp_path / "selected-target"
    target.mkdir()
    (target / ".git").mkdir()
    (target / "AGENTS.override.md").write_text("TARGET_OVERRIDE_ONLY", encoding="utf-8")
    (target / "AGENTS.md").write_text("TARGET_ORDINARY_MUST_NOT_WIN", encoding="utf-8")
    (host.workspace / "AGENTS.md").write_text("CONTROLLER_PROJECT_MUST_NOT_LEAK", encoding="utf-8")
    identity = host.environments.primary().selection.environment_id
    request = TurnEnvironmentRequest(identity, target.as_uri(), (target.as_uri(),))
    await host.owner.update_selections((TurnEnvironmentSelection.from_request(request),))
    captured = await host.owner.snapshot()
    host.context.run_context.turn_environments = captured
    builder = ContextBuilder(workspace_root=host.workspace)
    builder.bind_tool_context(host.context)
    try:
        await builder._ensure_environment_project_instructions()
        await builder._ensure_git_status_context(host.workspace)
        state = AgentState(user_message="Use the selected executor", workspace_root=host.workspace)
        prompt = "\n".join(message.content for message in builder._render_prompt_messages(state, host.workspace))
        assert "TARGET_OVERRIDE_ONLY" in prompt
        assert "CONTROLLER_PROJECT_MUST_NOT_LEAK" not in prompt
        assert "TARGET_ORDINARY_MUST_NOT_WIN" not in prompt
        assert builder._git_status_workspace == target.as_uri()
        assert "host_environment" not in {section["name"] for section in state.prompt_context["prompt_section_summary"]["sections"]}
        xml = builder._build_environment_context_xml(state)
        assert str(target) in xml and str(host.workspace) not in xml
        assert (await load_environment_project_instructions(captured, max_bytes=6)).text == "TARGET"
    finally:
        await captured.aclose()


@pytest.mark.asyncio
async def test_empty_captured_environment_prompt_never_uses_host_paths_or_shell(executor_host):
    host = executor_host
    host.context.run_context.turn_environments = TurnEnvironmentSnapshot()
    builder = ContextBuilder(workspace_root=host.workspace)
    builder.bind_tool_context(host.context)
    state = AgentState(user_message="Observe no executor", workspace_root=host.workspace)
    await builder._ensure_environment_project_instructions()
    await builder._ensure_git_status_context(host.workspace)
    xml = builder._build_environment_context_xml(state)
    assert "<environment " not in xml and "<environments>" not in xml
    assert "<cwd>" not in xml and "<shell>" not in xml and "<filesystem>" not in xml
    assert builder._git_status_context == ""


@pytest.mark.asyncio
async def test_project_docs_share_budget_across_original_ready_attachments(executor_host, tmp_path):
    host = executor_host
    source = os.environ.get("MINICODE_SOURCE_EXECUTOR")
    if source is None:
        pytest.skip("requires an explicitly selected owned Source exec-server peer for the second attachment")
    first, second = tmp_path / "first", tmp_path / "second"
    for directory, text in ((first, "FIRST_"), (second, "SECOND_")):
        directory.mkdir()
        (directory / ".git").mkdir()
        (directory / "AGENTS.md").write_text(text, encoding="utf-8")
    source_home = tmp_path / "second-source-home"
    source_home.mkdir()
    host.manager.upsert({"id": "second", "program": source,
        "args": ["exec-server", "--listen", "stdio", "--concurrent-requests", "4"],
        "cwd": str(second), "env": {"CODEX_HOME": str(source_home)}})
    identity = host.environments.primary().selection.environment_id
    await host.owner.update_selections(tuple(TurnEnvironmentSelection.from_request(TurnEnvironmentRequest(name,
        directory.as_uri(), (directory.as_uri(),))) for name, directory in ((identity, first), ("second", second))))
    async with await host.owner.snapshot() as snapshot:
        text = (await load_environment_project_instructions(snapshot, max_bytes=8)).text
        assert f"for `{identity}` with root" in text and "for `second` with root" in text
        assert "FIRST_" in text and text.endswith("SE") and "SECOND_" not in text


@pytest.mark.asyncio
async def test_compaction_restores_real_target_bytes_without_reauthorizing_discarded_reads(executor_host, tmp_path):
    host = executor_host
    target = tmp_path / "compaction-target"
    target.mkdir()
    (target / "same-name.txt").write_text("REAL_TARGET_RESTORED_BYTES", encoding="utf-8")
    (host.workspace / "same-name.txt").write_text("CONTROLLER_FILE_MUST_NOT_RESTORE", encoding="utf-8")
    identity = host.environments.primary().selection.environment_id
    await host.owner.update_selections((TurnEnvironmentSelection.from_request(TurnEnvironmentRequest(identity,
        target.as_uri(), (target.as_uri(),))),))
    captured = await host.owner.snapshot()
    host.context.run_context.turn_environments = captured
    builder = ContextBuilder(workspace_root=host.workspace)
    builder.bind_tool_context(host.context)
    host.builder = builder
    host.context.metadata["_executor_read_file_hashes"] = builder.executor_read_file_hashes()
    host.context.metadata["_executor_observed_files"] = builder.executor_observed_files()
    try:
        _, result, _ = await batch(host, "read_file", {"file_path": "same-name.txt"})
        assert not result["is_error"], result
        assert builder.executor_read_file_hashes() and builder.executor_observed_files()
        await builder._ensure_environment_project_instructions()
        builder._install_compacted_history([LLMMessage(role="user", content="Preserved summary")],
            removed_prefix=0, recent_count=0, restore_state=host.state)
        await builder._restore_executor_files_after_compaction(host.state)
        assert not builder.executor_read_file_hashes()
        assert builder._workspace_root == host.workspace
        notes = "\n".join(note["content"] for note in builder._persistent_notes)
        assert "REAL_TARGET_RESTORED_BYTES" in notes and "CONTROLLER_FILE_MUST_NOT_RESTORE" not in notes
        cold = ContextBuilder(workspace_root=host.workspace)
        cold.load_snapshot(builder.export_snapshot())
        assert cold.executor_read_file_hashes() == {} and cold.executor_observed_files() == {}
    finally:
        await captured.aclose()


@pytest.mark.asyncio
async def test_sdk_session_and_fork_keep_actual_attachment_after_same_id_registry_replacement(executor_host, tmp_path):
    host = executor_host
    model = CaptureModel()
    runtime = AgentRuntime(metrics_file=tmp_path / "sdk-runtime/metrics.jsonl", enable_lease_heartbeat=False)
    original = host.environments.primary().environment
    identity = host.environments.primary().selection.environment_id
    request = TurnEnvironmentRequest(identity, host.workspace.as_uri(), (host.workspace.as_uri(),))
    config = replace(host.snapshot.config, agent=AgentSettings(code_mode_only=False, max_iterations=2))
    parent = create_session(session_id="actual-env-parent", workspace_root=host.workspace,
        llm=model, config=config, tool_registry=create_tool_registry(), artifact_store=host.context.artifact_store,
        permission_context=host.context.permission, run_context=RunContext(agent_runtime=runtime),
        environment_manager=host.manager, environment_requests=(request,))
    child = None

    async def collect(stream):
        async with aclosing(stream):
            events = [event async for event in stream]
        assert next(event for event in reversed(events) if event.type == "done").data["status"] == "completed", events

    try:
        await collect(parent.query("Capture original executor"))
        assert parent._thread_environments is not None and not parent._thread_environments.closed
        child = parent.fork(session_id="actual-env-child")
        if identity != "local":
            replacement = host.manager.upsert({"id": identity, "program": "minicode-owned-unused-replacement"})
            assert replacement is not original
        await parent.aclose()
        await collect(child.query("Use the inherited original executor"))
        async with await child._thread_environments.snapshot() as captured:
            assert captured.primary().environment is original
        assert len(model.requests) == 2
        assert all(any("<environment_context>" in message.content for message in messages) for messages in model.requests)
    finally:
        if child is not None:
            await child.aclose()
        await parent.aclose()
        runtime.close(release_lease=True)
