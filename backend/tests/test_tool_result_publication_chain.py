from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agent import tool_result_persistence as persistence
from backend.agent.content_projection import normalise_content
from backend.agent.context import ContextBuilder
from backend.agent.diagnostic_store import DiagnosticPayloadStore
from backend.agent.provider_completion import ProviderCompletionCoordinator
from backend.agent.public_projection import project_public_usage, project_public_subagent_result
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.agent.stream_attempt import StreamAttemptState
from backend.agent.tool_execution import store_result
from backend.agent.turn_kernel import TurnKernel
from backend.artifact.store import ArtifactStore
from backend.config import TokenBudget
from backend.llm.base import StreamEvent, StreamEventType, ToolCallEvent, UsageInfo
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.base import ToolResult
from backend.tools.read_file import ReadFileTool


class _UnavailableArtifactStore:
    def save(self, **kwargs):
        raise OSError("fixture artifact publication failed")


@pytest.mark.parametrize("line_count", [2_001, 10_000])
@pytest.mark.asyncio
async def test_failed_artifact_publication_keeps_full_result_and_error_under_its_owner(tmp_path, monkeypatch, line_count):
    monkeypatch.setattr(persistence, "TOOL_RESULT_DATA_DIR", tmp_path / "tool-results")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = ToolExecutionContext(
        permission=PermissionContext(conversation_id="conv-owner", workspace_root=workspace),
        conversation_id="conv-owner", workspace_root=workspace, artifact_store=_UnavailableArtifactStore(),
    )
    state = AgentState(user_message="inspect")
    builder = ContextBuilder(token_budget=TokenBudget(total=200_000, response_reserve=1000))
    call = ToolCallEvent(id="call-full-output", name="fixture", arguments={})
    builder.append_user("inspect")
    builder.append_assistant_tool_calls([call])
    raw = "prefix\n" * line_count + "FULL_OUTPUT_TAIL_MARKER"
    event = store_result(call, ToolResult(content=raw), builder, state, tool_ctx=context)
    saved = next((tmp_path / "tool-results").glob("call-full-output_*.txt"))
    assert saved.read_text(encoding="utf-8") == raw
    snapshot = builder.export_snapshot()
    assert str(saved) in snapshot["history"][-1]["content"]
    restored = ContextBuilder(token_budget=TokenBudget(total=200_000, response_reserve=1000))
    restored.load_snapshot(snapshot)
    assert str(saved) in restored._history[-1].content
    assert str(saved) in state.tool_calls[-1].tool_output
    assert event.data["status"] == "success"
    assert "fixture artifact publication failed" in event.data["limitation"]
    assert str(saved) in event.data["summary"]
    reader = ReadFileTool(ArtifactStore(storage_dir=tmp_path / "reader-artifacts"))
    recovered = await reader.execute({"file_path": str(saved), "start_line": line_count, "end_line": line_count + 1}, context=context)
    assert not recovered.is_error and "FULL_OUTPUT_TAIL_MARKER" in recovered.content
    assert not persistence.is_tool_result_path(saved, conversation_id="conv-other", workspace_root=workspace)


def test_tool_result_directory_failure_uses_the_existing_preserve_inline_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "TOOL_RESULT_DATA_DIR", tmp_path / "tool-results")
    mkdir = Path.mkdir

    def unavailable(path, *args, **kwargs):
        if path == persistence.TOOL_RESULT_DATA_DIR:
            raise PermissionError("fixture directory is read-only")
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", unavailable)
    raw = "output\n" * 10000
    assert persistence.try_persist_tool_result(raw, "call-directory", "fixture") == raw
    assert not persistence.TOOL_RESULT_DATA_DIR.exists()


def test_both_output_stores_failing_produce_explicit_unavailable_retention(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "TOOL_RESULT_DATA_DIR", tmp_path / "tool-results")

    def unavailable(*args, **kwargs):
        raise PermissionError("fixture result publication failed")

    monkeypatch.setattr(persistence, "atomic_write_text", unavailable)
    context = ToolExecutionContext(permission=PermissionContext(), artifact_store=_UnavailableArtifactStore())
    call = ToolCallEvent(id="call-both-failed", name="fixture", arguments={})
    event = store_result(call, ToolResult(content="line\n" * 10000), ContextBuilder(), AgentState(user_message="inspect"), tool_ctx=context)
    assert "fixture artifact publication failed" in event.data["limitation"]
    assert "Full output could not be retained" in event.data["limitation"]
    assert "saved to" not in event.data["summary"]


@pytest.mark.asyncio
async def test_provider_diagnostics_keep_run_and_conversation_owners_and_reduced_references(tmp_path):
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", swarm_store_dir=tmp_path / "swarm")
    store = DiagnosticPayloadStore()
    produced = []
    for run_id, conversation_id in [("run-one", "conv-one"), ("run-two", "conv-one"), ("run-three", "conv-two")]:
        state = AgentState(user_message="inspect", conversation_id=conversation_id)
        kernel = TurnKernel.create(
            metadata={"run_id": run_id}, state=state, budget=TokenBudget(), task_id="", session_id="session",
            emit_event=None, initial_user_message="inspect", run_context=RunContext(agent_runtime=runtime),
        )
        coordinator = ProviderCompletionCoordinator(settings=SimpleNamespace(), state=state, turn_kernel=kernel, prompt_cache_tracking_source="audit", turn_started_at=0, turn_start_tool_call_count=0)
        stream = StreamAttemptState()
        completion = await coordinator.settle(
            StreamEvent(type=StreamEventType.DONE, usage=UsageInfo(), raw={"provider": "fixture", "model": run_id, "trace_id": "external-stale-id", "request_id": "vendor-request-id"}),
            stream_state=stream, provider_attempt=None, prompt_cache_safe_params={}, iteration_id="iter:1", iteration_limit=10, tool_batch_count=0,
        )
        trace = next(event for event in completion.events if event.type == "inspector.update")
        trace_id = trace.data["target_id"]
        assert trace_id == f"{run_id}:iter:1:provider:1"
        assert stream.raw_done["trace_id"] == trace_id
        assert stream.raw_done["request_id"] == "vendor-request-id"
        store.put("provider", trace_id, trace.data["payload"], conversation_id=conversation_id)
        reduced = store.put("provider", stream.raw_done["trace_id"], {"trace_id": trace_id, "model": run_id}, conversation_id=conversation_id)
        assert reduced["diagnostics_ref"] == f"provider:{trace_id}"
        produced.append((trace_id, run_id, conversation_id))
    for trace_id, run_id, conversation_id in produced:
        loaded = store.get("provider", trace_id)
        assert loaded.conversation_id == conversation_id and loaded.payload["model"] == run_id
    store.share_for_conversation("conv-one", "conv-fork")
    store.delete_for_conversation("conv-one")
    assert store.get("provider", produced[0][0]).conversation_ids == ("conv-fork",)
    assert store.get("provider", produced[2][0]).conversation_ids == ("conv-two",)


def test_content_and_public_result_boundaries_keep_text_zero_and_drop_runtime_fences():
    assert normalise_content([{"type": "text", "text": 0}]) == "0"
    usage = project_public_usage({"input_tokens": 0, "cost_usd": 0.0, "request_body": {"secret": "private"}})
    assert usage == {"input_tokens": 0, "cost_usd": 0.0}
    projected = project_public_subagent_result({"content": "done", "owner_nonce": "private", "usage": usage})
    assert projected["content"] == "done" and projected["usage"] == usage and "owner_nonce" not in projected
