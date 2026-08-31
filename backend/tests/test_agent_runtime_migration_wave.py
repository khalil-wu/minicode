from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.diagnostic_store import DiagnosticPayloadStore
from backend.agent.prompt_cache import _diff_request_summaries, build_prompt_cache_safe_params
from backend.agent.swarm_store import FileSwarmStore
from backend.artifact.store import ArtifactStore
from backend.llm.anthropic_adapter import AnthropicAdapter
from backend.llm.base import LLMMessage
from backend.tools.command_tool import RunCommandTool
from backend.ws.reasoning_batcher import ReasoningEventBatcher, ReasoningFlushDeadline
from backend.ws.handlers.misc import handle_inspector_focus
from backend.agent.message import AgentEvent
from backend.agent.state import AgentState
from backend.agent.tool_execution import store_result
from backend.llm.base import ToolCallEvent
from backend.tools.base import ToolResult


def test_run_command_classifies_each_invocation_from_arguments(tmp_path) -> None:
    tool = RunCommandTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))

    read = {"command": "git status --short"}
    workspace_write = {"command": "python -m pytest"}
    external = {"command": "pip install example-package"}
    destructive = {"command": "git reset --hard HEAD~1"}

    # Shell text is not a capability proof. Dedicated typed read/search tools
    # own the automatic read path; every shell invocation stays uncached and
    # outside concurrent speculative execution.
    assert tool.get_side_effect_kind(read) == "workspace"
    assert tool.is_idempotent(read) is False
    assert tool.is_concurrency_safe(read) is False
    assert tool.get_side_effect_kind(workspace_write) == "workspace"
    assert tool.is_concurrency_safe(workspace_write) is False
    assert tool.get_side_effect_kind(external) == "external"
    assert tool.get_side_effect_kind(destructive) == "destructive"


def test_powershell_format_cmdlets_are_not_destructive(tmp_path) -> None:
    tool = RunCommandTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))

    assert tool.get_side_effect_kind({
        "command": "Get-ChildItem -Recurse | Format-Table -AutoSize",
    }) == "workspace"
    assert tool.get_side_effect_kind({
        "command": "Get-ChildItem | Format-List Name,Length",
    }) == "workspace"
    assert tool.get_side_effect_kind({"command": "format disk"}) == "destructive"


def _claim_refs(claims: list[dict]) -> list[dict]:
    return [
        {
            "message_id": claim["message"]["message_id"],
            "participant_id": claim["participant_id"],
            "mailbox_epoch": claim["mailbox_epoch"],
            "claim_token": claim["claim_token"],
        }
        for claim in claims
    ]


def test_mailbox_claim_is_exclusive_releasable_expiring_and_epoch_fenced(tmp_path) -> None:
    store = FileSwarmStore(tmp_path / "swarm")
    store.append_message({
        "conversation_id": "conversation-a",
        "sender_id": "main",
        "recipient_id": "worker",
        "recipient_mailbox_epoch": 2,
        "content": "deliver exactly once",
    })

    first = store.claim_messages(
        participant_id="worker",
        mailbox_epoch=2,
        claim_owner="owner-a",
        now_ms=1_000,
        lease_ms=2_000,
    )
    assert len(first) == 1
    assert store.claim_messages(
        participant_id="worker",
        mailbox_epoch=2,
        claim_owner="owner-b",
        now_ms=1_500,
    ) == []
    assert store.claim_messages(
        participant_id="worker",
        mailbox_epoch=1,
        claim_owner="owner-old",
        now_ms=1_500,
    ) == []

    assert store.release_message_claims(_claim_refs(first), claim_owner="owner-a") == 1
    second = store.claim_messages(
        participant_id="worker",
        mailbox_epoch=2,
        claim_owner="owner-b",
        now_ms=2_000,
        lease_ms=1_000,
    )
    assert len(second) == 1
    expired = store.claim_messages(
        participant_id="worker",
        mailbox_epoch=2,
        claim_owner="owner-c",
        now_ms=3_001,
    )
    assert len(expired) == 1
    assert store.ack_message_claims(_claim_refs(expired), claim_owner="owner-c") == 1
    assert store.claim_messages(
        participant_id="worker",
        mailbox_epoch=2,
        claim_owner="owner-d",
        now_ms=4_000,
    ) == []


def test_prompt_cache_diagnostic_locates_first_message_divergence() -> None:
    previous = build_prompt_cache_safe_params(
        messages=[LLMMessage(role="system", content="stable"), LLMMessage(role="user", content="one")],
        tool_schemas=[],
    )
    current = build_prompt_cache_safe_params(
        messages=[LLMMessage(role="system", content="stable"), LLMMessage(role="user", content="two")],
        tool_schemas=[],
    )

    changes, details = _diff_request_summaries(previous, current)

    assert "message prefix changed" in changes
    assert details["message_prefix_delta"]["common_message_count"] == 1
    assert details["message_prefix_delta"]["first_diverging_index"] == 1
    assert "content_hash" in details["message_prefix_delta"]["changed_fields"]


def test_diagnostic_store_defers_full_payload_and_replaces_by_identity() -> None:
    store = DiagnosticPayloadStore(max_entries=2, max_bytes=100_000)
    payload = {
        "kind": "provider_trace",
        "provider": "anthropic",
        "usage": {"input_tokens": 100},
        "request_summary": {"model": "claude", "message_shadows": [{"hash": "a"}] * 20},
        "provider_timeline": [{"event": "delta", "raw": "x" * 1000}],
    }

    compact = store.put("provider", "trace-1", payload, conversation_id="conversation-a")
    loaded = store.get("provider", "trace-1")

    assert compact["diagnostics_deferred"] is True
    assert "provider_timeline" not in compact
    assert "message_shadows" not in compact["request_summary"]
    assert loaded is not None
    assert loaded.payload == payload
    assert loaded.conversation_id == "conversation-a"


def test_diagnostic_store_does_not_downgrade_an_authoritative_provider_trace() -> None:
    store = DiagnosticPayloadStore(max_entries=2, max_bytes=100_000)
    trace = {
        "kind": "provider_trace",
        "provider": "custom",
        "model": "claude-opus-audit",
        "finish_reason": "end_turn",
        "provider_timeline": [{"event": "message_stop"}],
    }
    store.put("provider", "iter:1:provider:1", trace, conversation_id="conversation-a")

    compact_done = store.put(
        "provider",
        "iter:1:provider:1",
        {
            "provider": "custom",
            "trace_id": "iter:1:provider:1",
            "finish_reason": "end_turn",
        },
        conversation_id="conversation-a",
    )
    loaded = store.get("provider", "iter:1:provider:1")

    assert compact_done["diagnostics_deferred"] is True
    assert compact_done["finish_reason"] == "end_turn"
    assert loaded is not None
    assert loaded.payload == trace


def test_anthropic_cache_editing_is_explicit_pinned_and_strippable() -> None:
    disabled = AnthropicAdapter(api_key="test")
    enabled = AnthropicAdapter(api_key="test", cache_editing_beta_header="provider-cache-edit-beta")
    assert disabled.capabilities.native_cache_editing is False
    assert disabled.queue_cache_deletions(["tool-1"]) is False
    assert enabled.capabilities.native_cache_editing is True
    assert enabled.queue_cache_deletions(["tool-1"]) is True

    messages = [
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "large"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "observed"}]},
        {"role": "user", "content": [{"type": "text", "text": "continue"}]},
    ]
    edited, _, pin = enabled._add_cache_breakpoints(
        messages,
        cache_editing=True,
        new_cache_deletions=("tool-1",),
    )

    assert edited[0]["content"][0]["cache_reference"] == "tool-1"
    assert any(block.get("type") == "cache_edits" for block in edited[2]["content"])
    assert pin is not None

    next_messages = [*messages, {"role": "assistant", "content": "done"}, {"role": "user", "content": "next"}]
    replayed, _, _ = enabled._add_cache_breakpoints(
        next_messages,
        cache_editing=True,
        pinned_cache_edits=(pin,),
    )
    assert any(block.get("type") == "cache_edits" for block in replayed[2]["content"])



def test_anthropic_cache_editing_state_is_conversation_scoped() -> None:
    adapter = AnthropicAdapter(
        api_key="test",
        cache_editing_beta_header="provider-cache-edit-beta",
    )

    assert adapter.queue_cache_deletions(["tool-a"], conversation_id="conversation-a")
    state_a = adapter._cache_editing_state(
        {"conversation_id": "conversation-a"}
    )
    state_b = adapter._cache_editing_state(
        {"conversation_id": "conversation-b"}
    )
    assert state_a.pending_deletions == ["tool-a"]
    assert state_b.pending_deletions == []

    state_a.disabled_reason = "provider_rejected_cache_editing_request"
    assert not adapter.queue_cache_deletions(
        ["tool-a-2"],
        conversation_id="conversation-a",
    )
    assert adapter.queue_cache_deletions(
        ["tool-b"],
        conversation_id="conversation-b",
    )
    assert state_b.pending_deletions == ["tool-b"]

    assert adapter.queue_cache_deletions(["tool-default"])
    default_state = adapter._cache_editing_state()
    assert default_state.pending_deletions == ["tool-default"]

    asyncio.run(adapter.aclose())
    assert adapter._cache_editing_states == {}


def test_anthropic_background_fork_marks_shared_prefix_instead_of_child_tail() -> None:
    adapter = AnthropicAdapter(api_key="test")
    messages = [
        {"role": "user", "content": "shared user turn"},
        {"role": "assistant", "content": "shared assistant turn"},
        {"role": "user", "content": "background child directive"},
    ]

    normal, _, _ = adapter._add_cache_breakpoints(messages)
    background, _, _ = adapter._add_cache_breakpoints(
        messages,
        skip_cache_write=True,
    )

    assert normal[2]["content"][-1]["cache_control"]["type"] == "ephemeral"
    assert background[1]["content"][-1]["cache_control"]["type"] == "ephemeral"
    assert all(
        "cache_control" not in block
        for block in background[2]["content"]
        if isinstance(block, dict)
    )


def test_reasoning_batcher_preserves_content_and_flushes_before_boundary() -> None:
    batcher = ReasoningEventBatcher(max_chars=20, max_delay_seconds=1.0)
    first = AgentEvent.thinking_chunk("first ", source="provider", phase="model")
    second = AgentEvent.thinking_chunk("second", source="provider", phase="model")

    assert batcher.push(first, now=1.0) == [first]
    assert batcher.push(second, now=1.1) == []
    boundary_flush = batcher.flush_if_pending()

    assert boundary_flush is not None
    assert boundary_flush.data["content"] == "second"
    assert batcher.flush_if_pending() is None


@pytest.mark.asyncio
async def test_reasoning_deadline_flushes_without_a_followup_event_and_closes_cleanly() -> None:
    flushed = asyncio.Event()
    batcher = ReasoningEventBatcher(max_chars=100, max_delay_seconds=0.01)
    sent: list[AgentEvent] = []

    async def flush_pending() -> None:
        pending = batcher.flush_if_pending()
        if pending is not None:
            sent.append(pending)
        flushed.set()

    deadline = ReasoningFlushDeadline(0.01, flush_pending)
    first = AgentEvent.thinking_chunk("first", source="provider", phase="model")
    assert batcher.push(first) == [first]
    assert batcher.push(
        AgentEvent.thinking_chunk(" buffered", source="provider", phase="model")
    ) == []
    deadline.arm()

    await asyncio.wait_for(flushed.wait(), timeout=0.2)
    assert [event.data["content"] for event in sent] == [" buffered"]

    assert batcher.push(
        AgentEvent.thinking_chunk("must not leak", source="provider", phase="model")
    ) == []
    deadline.arm()
    await deadline.close()
    await asyncio.sleep(0.02)
    assert [event.data["content"] for event in sent] == [" buffered"]
    assert batcher.flush_if_pending().data["content"] == "must not leak"


def test_reasoning_batcher_flushes_old_stream_before_metadata_change() -> None:
    batcher = ReasoningEventBatcher(max_chars=100, max_delay_seconds=1.0)
    raw = AgentEvent.thinking_chunk("raw", source="provider", phase="model")
    assert batcher.push(raw, now=1.0) == [raw]

    emitted = batcher.push(
        AgentEvent.thinking_chunk("summary", source="provider", phase="final"),
        now=1.1,
    )

    assert [event.data["content"] for event in emitted] == ["summary"]
    assert batcher.flush_if_pending() is None


def test_reasoning_batcher_preserves_lifecycle_boundaries() -> None:
    batcher = ReasoningEventBatcher(max_chars=100, max_delay_seconds=1.0)
    started = AgentEvent.thinking_chunk(
        "",
        item_id="reasoning-1",
        content_index=0,
        lifecycle="start",
    )
    delta = AgentEvent.thinking_chunk(
        "step",
        item_id="reasoning-1",
        content_index=0,
        lifecycle="delta",
    )
    ended = AgentEvent.thinking_chunk(
        "",
        item_id="reasoning-1",
        content_index=0,
        lifecycle="end",
    )

    assert batcher.push(started, now=1.0) == [started]
    assert batcher.push(delta, now=1.1) == [delta]
    assert batcher.push(ended, now=1.2) == [ended]
    assert batcher.flush_if_pending() is None


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("conversation_id", "conversation-2"),
        ("message_id", "assistant-2"),
        ("task_id", "task-2"),
        ("turn_id", "turn-2"),
        ("item_id", "reasoning-2"),
        ("content_index", 1),
    ],
)
def test_reasoning_batcher_never_merges_across_owner_or_item_identity(
    field: str,
    replacement: object,
) -> None:
    batcher = ReasoningEventBatcher(max_chars=100, max_delay_seconds=1.0)
    base = AgentEvent.thinking_chunk(
        "first",
        item_id="reasoning-1",
        content_index=0,
        lifecycle="delta",
    )
    base.data.update({
        "conversation_id": "conversation-1",
        "message_id": "assistant-1",
        "task_id": "task-1",
        "turn_id": "turn-1",
    })
    changed = AgentEvent(type=base.type, data={**base.data, "content": "second", field: replacement})

    assert batcher.push(base, now=1.0) == [base]
    assert batcher.push(changed, now=1.1) == [changed]
    assert batcher.flush_if_pending() is None


def test_reasoning_batcher_never_merges_different_event_types() -> None:
    batcher = ReasoningEventBatcher(max_chars=100, max_delay_seconds=1.0)
    delta = AgentEvent.thinking_chunk("delta", lifecycle="delta")
    legacy = AgentEvent(type="thinking", data=dict(delta.data))

    assert batcher.push(delta, now=1.0) == [delta]
    assert batcher.push(legacy, now=1.1) == [legacy]
    assert batcher.flush_if_pending() is None


def test_repeated_empty_search_is_reported_as_executed_not_blocked() -> None:
    state = AgentState(user_message="search twice")
    arguments = {"query": "definitely_missing_symbol"}
    state.record_tool_call("grep_files", arguments, "No matches found", status="success")
    call = ToolCallEvent(id="grep-2", name="grep_files", arguments=arguments)

    event = store_result(
        call,
        ToolResult(content="No matches found"),
        ctx=ContextBuilder(),
        state=state,
    )

    assert event.data["status"] == "success"
    assert event.data["summary"] == "No matches found"
    assert len(state.tool_calls) == 2


@pytest.mark.asyncio
async def test_inspector_focus_loads_the_server_side_full_payload() -> None:
    class Session:
        active_conversation_id = "conversation-a"

        def __init__(self) -> None:
            self.diagnostic_store = DiagnosticPayloadStore()
            self.events: list[AgentEvent] = []
            self.ws_manager = None
            self.session_lifecycle = SimpleNamespace(
                current_workspace_root=lambda: None,
                workspace_root=None,
            )

        @staticmethod
        def resolve_requested_workspace(requested_workspace: str | None = None) -> Path:
            return Path(requested_workspace or ".").expanduser().resolve()

        async def send_event(self, event: AgentEvent) -> None:
            self.events.append(event)

    session = Session()
    session.diagnostic_store.put(
        "provider",
        "trace-1",
        {"provider_timeline": [{"event": "done"}], "request_summary": {"model": "test"}},
        conversation_id="conversation-a",
    )

    handled = await handle_inspector_focus(
        session,  # type: ignore[arg-type]
        {"target_kind": "provider", "target_id": "trace-1"},
    )

    assert handled is True
    assert session.events[0].data["diagnostics_loaded"] is True
    assert session.events[0].data["payload"]["diagnostics_deferred"] is False
    assert session.events[0].data["payload"]["provider_timeline"] == [{"event": "done"}]
