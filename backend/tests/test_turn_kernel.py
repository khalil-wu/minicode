from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agent.runtime import AgentRuntime
from backend.agent.run_context import RunContext
from backend.agent.state import AgentState
from backend.agent.turn_input import TurnInputQueue
from backend.agent.turn_kernel import PermissionContextRefreshError, TurnKernel
from backend.agent.message import AgentEvent
from backend.config import TokenBudget
from backend.permissions.context import PermissionContext, ToolExecutionContext


def _kernel(
    tmp_path: Path,
    *,
    metadata: dict | None = None,
    emit_event=None,
    initial_user_message: str = "initial",
    turn_input_queue=None,
    persist_consumed_turn_input=None,
    connected_mcp_servers: tuple[str, ...] = (),
) -> TurnKernel:
    runtime = AgentRuntime(
        metrics_file=tmp_path / "metrics.jsonl",
        swarm_store_dir=tmp_path / "swarm",
    )
    resolved_metadata = dict(metadata or {})
    run_context = RunContext(
        agent_runtime=runtime,
        turn_input_queue=turn_input_queue,
        persist_consumed_turn_input=persist_consumed_turn_input,
        connected_mcp_servers=connected_mcp_servers,
    )
    state = AgentState(user_message=initial_user_message)
    state.conversation_id = "conv"
    return TurnKernel.create(
        metadata=resolved_metadata,
        state=state,
        budget=TokenBudget(),
        task_id="task",
        session_id="session",
        emit_event=emit_event,
        initial_user_message=initial_user_message,
        run_context=run_context,
    )


def test_turn_kernel_owns_runtime_start_and_idempotent_completion(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)

    start_events = kernel.start_events()
    assert [event.type for event in start_events] == ["agent.run.started"]
    completed = kernel.complete_for_terminal_reason("completed")
    duplicate = kernel.complete_for_terminal_reason("completed")

    assert completed is not None
    assert completed.type == "agent.run.completed"
    assert duplicate is None
    assert kernel.completion_emitted is True
    assert kernel.run_record.status == "completed"


def test_success_lifecycle_does_not_encode_an_empty_error_object() -> None:
    event = AgentEvent.agent_run_completed({
        "run_id": "run-completed",
        "status": "completed",
        "result": {"content": "done"},
    })

    assert event.data["status"] == "completed"
    assert "error" not in event.data


def test_turn_kernel_consumes_initial_input_before_queued_steer(tmp_path: Path) -> None:
    queue = TurnInputQueue()
    command = SimpleNamespace(
        data={
            "content": "steered",
            "assistant_message_id": "assistant-steer",
            "user_message_id": "user-steer",
            "attachments": [{"name": "evidence.txt"}],
        }
    )
    queued = queue.enqueue_command(command, target_message_id="assistant-initial")
    persisted: list[str] = []

    async def persist(item) -> None:
        persisted.append(item.message_id)

    kernel = _kernel(
        tmp_path,
        turn_input_queue=queue,
        persist_consumed_turn_input=persist,
    )

    async def consume():
        initial = await kernel.take_boundary_input(initial_turn_pending=True)
        steered = await kernel.take_boundary_input(initial_turn_pending=False)
        return initial, steered

    initial, steered = asyncio.run(consume())

    assert initial.content == "initial"
    assert initial.consumed_steer is None
    assert queue.pending_count() == 0
    assert queued is not None
    assert steered.content == "steered"
    assert steered.attachments == ({"name": "evidence.txt"},)
    assert steered.consumed_steer == queued
    assert persisted == ["assistant-steer"]


def test_turn_kernel_applies_structured_context_from_steer(monkeypatch, tmp_path: Path) -> None:
    from backend.services import plugin_settings_service

    monkeypatch.setattr(
        plugin_settings_service,
        "get_plugin_settings",
        lambda: {
            "plugins": [{
                "name": "docs",
                "displayName": "Docs",
                "enabled": True,
                "skill_count": 0,
                "mcp_server_names": ["docs-search"],
            }],
        },
    )
    queue = TurnInputQueue()
    queue.enqueue_command(SimpleNamespace(data={
        "content": "use selected capabilities",
        "skills": [{"name": "review", "path": "C:/skills/review/SKILL.md"}],
        "plugins": [{"config_name": "docs", "path": "plugin://docs"}],
    }))
    kernel = _kernel(
        tmp_path,
        turn_input_queue=queue,
        connected_mcp_servers=("docs-search",),
    )

    async def consume():
        await kernel.take_boundary_input(initial_turn_pending=True)
        return await kernel.take_boundary_input(initial_turn_pending=False)

    boundary = asyncio.run(consume())

    assert boundary.content == "use selected capabilities"
    assert kernel.state.prompt_context["selected_skills"] == [{
        "name": "review",
        "path": "C:/skills/review/SKILL.md",
    }]
    assert kernel.state.prompt_context["plugin_injections"][0]["config_name"] == "docs"
    assert kernel.state.prompt_context["plugin_injections"][0]["mcp_server_names"] == ["docs-search"]


def test_turn_kernel_refreshes_live_permission_at_safe_boundary(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    current = PermissionContext(mode="plan", source="session")
    tool_context = ToolExecutionContext(
        permission=PermissionContext(mode="bypass"),
        allow_network=True,
    )

    def commit_permission(permission: PermissionContext):
        policy = SimpleNamespace(allow_network=False)
        tool_context.permission = permission
        tool_context.sandbox_policy = policy
        tool_context.allow_network = policy.allow_network
        return permission, policy

    kernel.run_context.permission_context_provider = lambda: current
    tool_context.run_context = kernel.run_context
    tool_context.permission_context_committer = commit_permission
    kernel.bind_tool_context(tool_context)

    assert kernel.refresh_live_permission_context() is True
    assert tool_context.permission == current
    assert tool_context.allow_network is False
    assert kernel.refresh_live_permission_context() is False


def test_turn_kernel_refresh_fails_closed_without_committer(tmp_path: Path) -> None:
    # Subagent tool contexts carry a permission_context_provider without a
    # turn-owned committer; the refresh must fail closed by stopping the turn
    # instead of continuing with the stale policy.
    kernel = _kernel(tmp_path)
    current = PermissionContext(mode="plan", source="session")
    tool_context = ToolExecutionContext(
        permission=PermissionContext(mode="bypass"),
        allow_network=True,
    )
    kernel.run_context.permission_context_provider = lambda: current
    tool_context.run_context = kernel.run_context
    kernel.bind_tool_context(tool_context)

    with pytest.raises(PermissionContextRefreshError):
        kernel.refresh_live_permission_context()
    assert tool_context.permission == PermissionContext(mode="bypass")


def test_turn_kernel_refresh_failure_does_not_return_stale_policy(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    tool_context = ToolExecutionContext(
        permission=PermissionContext(mode="bypass"),
    )
    tool_context.run_context = kernel.run_context
    tool_context.permission_context_committer = lambda _current: None
    kernel.run_context.permission_context_provider = lambda: (_ for _ in ()).throw(
        RuntimeError("session policy unavailable")
    )
    kernel.bind_tool_context(tool_context)

    with pytest.raises(PermissionContextRefreshError):
        kernel.refresh_live_permission_context()
    assert tool_context.permission.mode == "bypass"


def test_turn_kernel_starts_turn_for_attachment_only_steer(tmp_path: Path) -> None:
    queue = TurnInputQueue()
    queued = queue.enqueue_command(
        SimpleNamespace(
            data={
                "content": "",
                "assistant_message_id": "assistant-attachment",
                "attachments": [{"name": "trace.log"}],
            }
        )
    )
    kernel = _kernel(tmp_path, turn_input_queue=queue)

    async def consume():
        await kernel.take_boundary_input(initial_turn_pending=True)
        return await kernel.take_boundary_input(initial_turn_pending=False)

    attachment_input = asyncio.run(consume())

    assert queued is not None
    assert attachment_input.content == ""
    assert attachment_input.attachments == ({"name": "trace.log"},)
    assert attachment_input.should_start_turn is True


def test_preaccepted_attachment_steer_is_not_overwritten_by_next_fifo_item(
    tmp_path: Path,
) -> None:
    queue = TurnInputQueue()
    first = queue.enqueue_command(
        SimpleNamespace(
            data={
                "content": "",
                "assistant_message_id": "assistant-attachment",
                "attachments": [{"name": "trace.log"}],
            }
        )
    )
    second = queue.enqueue_command(
        SimpleNamespace(
            data={
                "content": "next",
                "assistant_message_id": "assistant-next",
            }
        )
    )
    kernel = _kernel(tmp_path, turn_input_queue=queue)

    async def consume():
        await kernel.take_boundary_input(initial_turn_pending=True)
        assert first is not None
        assert kernel.pop_turn_steer() is first
        await kernel.accept_turn_steer(first)
        return await kernel.take_boundary_input(initial_turn_pending=False)

    attachment_input = asyncio.run(consume())

    assert second is not None
    assert attachment_input.consumed_steer is first
    assert attachment_input.attachments == ({"name": "trace.log"},)
    assert queue.pending_count() == 1
    assert queue.snapshot()[0] is second


def test_turn_kernel_projects_runtime_span_with_stable_identity(tmp_path: Path) -> None:
    emitted: list[tuple[str, dict]] = []

    async def emit_event(event_type: str, payload: dict) -> None:
        emitted.append((event_type, payload))

    kernel = _kernel(
        tmp_path,
        metadata={"turn_id": "turn-1", "agent_role": "main"},
        emit_event=emit_event,
    )

    asyncio.run(
        kernel.emit_runtime_span(
            "provider.started",
            span_id="provider:1",
            iteration_id="iteration:1",
            phase="provider",
        )
    )

    assert emitted[0][0] == "runtime.span"
    payload = emitted[0][1]
    assert payload["run_id"] == kernel.run_record.run_id
    assert payload["turn_id"] == "turn-1"
    assert payload["iteration_id"] == "iteration:1"
    assert payload["span_id"] == "provider:1"


def test_agent_progress_validates_typed_provider_state() -> None:
    event = AgentEvent.progress(
        "模型正在响应",
        id="provider:connection:run:iteration",
        phase="model",
        provider_state="responding",
    )
    assert event.data["provider_state"] == "responding"
    with pytest.raises(ValueError, match="provider progress state"):
        AgentEvent.progress(
            "invalid",
            id="provider:connection:run:iteration",
            phase="model",
            provider_state="waiting",
        )


def test_provider_attempts_reset_first_byte_and_close_exactly_once(tmp_path: Path) -> None:
    emitted: list[tuple[str, dict]] = []

    async def emit_event(event_type: str, payload: dict) -> None:
        emitted.append((event_type, payload))

    kernel = _kernel(tmp_path, emit_event=emit_event)

    async def scenario():
        first = await kernel.start_provider_attempt(
            iteration_id="iteration:1",
            retry_index=0,
            started_at=100,
            max_retries=1,
        )
        first_wait = await kernel.observe_provider_first_event(
            first,
            progress_origin_ms=90,
            observed_at=120,
        )
        duplicate_wait = await kernel.observe_provider_first_event(
            first,
            progress_origin_ms=90,
            observed_at=125,
        )
        first_closed = await kernel.close_provider_attempt(
            first,
            status="failed",
            summary="retrying",
            ended_at=130,
        )
        duplicate_close = await kernel.close_provider_attempt(
            first,
            status="failed",
            summary="duplicate",
            ended_at=140,
        )
        second = await kernel.start_provider_attempt(
            iteration_id="iteration:1",
            retry_index=1,
            started_at=200,
            max_retries=1,
        )
        second_wait = await kernel.observe_provider_first_event(
            second,
            progress_origin_ms=190,
            observed_at=230,
        )
        await kernel.close_provider_attempt(
            second,
            status="completed",
            summary="done",
            ended_at=250,
        )
        return first, second, first_wait, duplicate_wait, second_wait, first_closed, duplicate_close

    first, second, first_wait, duplicate_wait, second_wait, first_closed, duplicate_close = asyncio.run(
        scenario()
    )

    assert first.first_byte_at == 120
    assert second.first_byte_at == 230
    assert first_wait == 30
    assert duplicate_wait is None
    assert second_wait == 40
    assert first_closed is True
    assert duplicate_close is False
    first_event_spans = [
        payload
        for event_type, payload in emitted
        if event_type == "runtime.span" and payload.get("event") == "provider.first_event"
    ]
    assert [payload["data"]["stream_attempt"] for payload in first_event_spans] == [1, 2]
    terminal_spans = [
        payload["event"]
        for event_type, payload in emitted
        if event_type == "runtime.span"
        and payload.get("event") in {"provider.request.failed", "provider.request.completed"}
    ]
    assert terminal_spans == ["provider.request.failed", "provider.request.completed"]
    progress_events = [
        payload
        for event_type, payload in emitted
        if event_type == "agent.progress"
    ]
    assert [payload["provider_state"] for payload in progress_events] == [
        "connecting",
        "responding",
        "failed",
        "reconnecting",
        "responding",
        "completed",
    ]
    assert all(payload["visibility"] == "debug" for payload in progress_events)
    assert progress_events[3]["retry_attempt"] == 1
    assert progress_events[3]["max_retries"] == 1


def test_turn_kernel_owns_provider_call_sequence(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)

    assert kernel.next_provider_call_count == 1
    assert kernel.commit_provider_call("iteration:1") == (1, "iteration:1:provider:1")
    assert kernel.next_provider_call_count == 2
    assert kernel.commit_provider_call("iteration:2") == (2, "iteration:2:provider:2")


def test_provider_progress_identity_is_scoped_to_the_iteration(tmp_path: Path) -> None:
    emitted: list[tuple[str, dict]] = []

    async def emit_event(event_type: str, payload: dict) -> None:
        emitted.append((event_type, payload))

    kernel = _kernel(tmp_path, emit_event=emit_event)

    async def scenario() -> None:
        first = await kernel.start_provider_attempt(
            iteration_id="iter:1",
            retry_index=0,
            started_at=100,
        )
        await kernel.close_provider_attempt(
            first,
            status="completed",
            summary="first iteration done",
            ended_at=120,
        )
        await kernel.start_provider_attempt(
            iteration_id="iter:2",
            retry_index=0,
            started_at=200,
        )

    asyncio.run(scenario())

    progress_ids = [
        payload["id"]
        for event_type, payload in emitted
        if event_type == "agent.progress"
    ]
    assert progress_ids == [
        "provider:connection:iter:1",
        "provider:connection:iter:1",
        "provider:connection:iter:2",
    ]


def test_turn_kernel_finalizes_checkpoint_by_terminal_reason(
    tmp_path: Path,
    monkeypatch,
) -> None:
    saved: list[dict] = []
    cleared: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "backend.agent.turn_kernel.save_run_checkpoint",
        lambda **kwargs: saved.append(kwargs),
    )
    monkeypatch.setattr(
        "backend.agent.turn_kernel.clear_checkpoints",
        lambda session_id, *, conversation_id="": cleared.append((session_id, conversation_id)),
    )
    kernel = _kernel(tmp_path)
    state = AgentState(user_message="work")
    state.conversation_id = "conv"
    state.stopped_reason = "timeout"
    context = SimpleNamespace(
        export_snapshot=lambda **_kwargs: {"history": [{"role": "user"}]}
    )

    assert kernel.finalize_checkpoint(
        session_id="session",
        user_message="work",
        state=state,
        context_builder=context,
    ) == "saved"
    assert saved[0]["run_id"] == kernel.run_record.run_id
    assert saved[0]["messages"] == [{"role": "user"}]
    assert saved[0]["context_snapshot"] == {"history": [{"role": "user"}]}
    assert isinstance(saved[0]["receipt"], dict)

    state.stopped_reason = "completed"
    assert kernel.finalize_checkpoint(
        session_id="session",
        user_message="work",
        state=state,
        context_builder=context,
    ) == "cleared"
    assert cleared == [("session", "conv")]
