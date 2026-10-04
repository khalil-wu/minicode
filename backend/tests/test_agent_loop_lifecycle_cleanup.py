from __future__ import annotations

import asyncio
import time
from contextlib import aclosing
from types import SimpleNamespace

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.conversation_query_guard import conversation_query_guards
from backend.agent.lifecycle_observer import LifecycleObserverOwner
from backend.agent.loop import AgentLoopSessionContext
from backend.agent.message import AgentEvent
from backend.agent.nested_tool_events import NestedToolEvents
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.query_chain import QueryChainTracking
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.agent.turn_budget import TurnBudgetController
from backend.agent.turn_iteration_admission import TurnIterationAdmission
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, AppConfig, LLMSettings, PermissionSettings, TokenBudget
from backend.conversations.repository import ConversationRepository
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.base import BaseTool, ToolResult, ToolSchema
from backend.tools.registry import ToolRegistry


@pytest.fixture
def short_cleanup(monkeypatch):
    monkeypatch.setattr("backend.agent.loop_preflight._PREFLIGHT_DRAIN_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr("backend.agent.nested_tool_events._CALLBACK_DRAIN_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr("backend.agent.lifecycle_observer._OBSERVER_FINISH_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr("backend.agent.tool_execution.CANCELLATION_DRAIN_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr("backend.tools.registry.CANCELLATION_DRAIN_TIMEOUT_SECONDS", 0.01)


async def _settle(owner):
    deadline = time.monotonic() + 2
    while pending := {task for task in owner.lifecycle_cleanup_tasks if not task.done()}:
        assert time.monotonic() < deadline, "released callback did not settle"
        await asyncio.wait(pending, timeout=0.05)
    await asyncio.sleep(0)
    assert not owner.lifecycle_cleanup_tasks


async def _claim_released(conversation_id):
    guards = conversation_query_guards()
    deadline = time.monotonic() + 2
    while guards.active_claim(conversation_id) is not None:
        assert time.monotonic() < deadline, "actual borrower settled but claim remained held"
        await asyncio.sleep(0.001)


class _LLM(LLMAdapter):
    async def stream_chat(self, messages, tools=None):
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="accepted answer")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages, **kwargs):
        return "late model use"


def _submission(tmp_path, owner, *, observer_factory=None, skill_manager=None):
    return QuerySubmission(
        user_message="inspect",
        session=AgentSession(
            llm=_LLM(), tool_registry=ToolRegistry(),
            artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
            permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
            agent_settings=AgentSettings(max_iterations=2), token_budget=TokenBudget(),
            context_builder=ContextBuilder(), lifecycle_observer_factory=observer_factory,
        ),
        runtime=AgentLoopSessionContext(
            workspace_root=tmp_path, run_context=owner, skill_manager=skill_manager,
        ),
    )


@pytest.mark.parametrize("boundary", ["deadline", "event", "task"])
def test_actual_admission_retains_anext_and_closes_only_after_settlement(
    tmp_path, short_cleanup, boundary,
):
    async def scenario():
        entered, release, acknowledged = asyncio.Event(), asyncio.Event(), asyncio.Event()
        effects, loans = [], []
        llm = _LLM()
        owner = RunContext(retain_model=lambda adapter, task: loans.append((adapter, task)))
        cancel = asyncio.Event()
        state = AgentState(user_message="inspect", max_iterations=2)

        async def prepare(**kwargs):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                acknowledged.set()
                await release.wait()
            effects.append("late preparation mutation")
            return SimpleNamespace(terminal=True, tool_schema_state=None, tool_schemas=[], events=[])

        async def apply_boundary(boundary):
            state.terminal_status = "partial"
            state.stopped_reason = "max_turn_seconds"
            return None, []

        deadline = None
        admission = TurnIterationAdmission(
            context=ContextBuilder(), state=state, llm=llm,
            iteration_runtime=SimpleNamespace(prepare=prepare, llm=llm, agent_session=None),
            deadline_controller=SimpleNamespace(elapsed=lambda: 0),
            turn_budget_controller=TurnBudgetController.from_settings(AgentSettings(max_iterations=2), max_iterations=2),
            budget_runtime=SimpleNamespace(
                local_tokens_used=lambda: 0, turn_cost_usd=lambda: 0, rollout_boundary=lambda: None,
                active_phase_deadline=lambda: deadline, phase_deadline_boundary=lambda: "deadline",
                apply_boundary=apply_boundary,
            ),
            turn_start_tool_call_count=0, chain=QueryChainTracking(user_message_preview="inspect"),
            tool_context=SimpleNamespace(cancel_event=cancel, run_context=owner, model_execution=None),
            token_budget=TokenBudget(), metadata={}, external_metadata=None, emit_event=None,
            runtime=None, run_record=SimpleNamespace(run_id="admission"), llm_request_metadata={},
            turn_kernel=None,
        )

        async def collect():
            return [update async for update in admission.admit(
                previous_tool_schema_state=None, initial_turn_pending=True, pending_turn_context=[],
            )]

        # Fixture construction is not part of the production admission budget.
        deadline = time.monotonic() + 0.03 if boundary == "deadline" else None
        caller = asyncio.create_task(collect())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            if boundary == "event":
                cancel.set()
            elif boundary == "task":
                caller.cancel()
            if boundary == "deadline":
                updates = await asyncio.wait_for(asyncio.shield(caller), 1)
                assert [item.action for item in updates] == ["terminate"]
            else:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(caller), 1)
            assert acknowledged.is_set()
            assert effects == []
            assert len(owner.lifecycle_cleanup_tasks) == 2
            assert any(not receipt["completed"] for receipt in owner.lifecycle_cleanup_receipts.values())
            assert {adapter for adapter, _task in loans} == {llm}
            assert owner.lifecycle_cleanup_tasks <= {task for _adapter, task in loans}
        finally:
            release.set()
            await _settle(owner)
        assert effects == ["late preparation mutation"]
        assert all(receipt["completed"] for receipt in owner.lifecycle_cleanup_receipts.values())

    asyncio.run(scenario())


def test_nested_callback_cancellation_returns_while_actual_callback_is_retained(short_cleanup):
    async def scenario():
        owner = RunContext()
        entered, release = asyncio.Event(), asyncio.Event()
        nested = NestedToolEvents(SimpleNamespace(record_event=lambda event: None), run_context=owner)

        async def callback():
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()

        async def consume():
            async with aclosing(nested.run_callback(callback())) as updates:
                return [update async for update in updates]

        caller = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(caller), 1)
            assert owner.lifecycle_cleanup_evidence()["lifecycle_cleanup_pending_count"] == 1
            assert nested._closed
        finally:
            release.set()
            await _settle(owner)

    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["deadline", "task"])
def test_observer_finish_has_hard_bound_and_finished_means_real_task_settled(short_cleanup, boundary):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        class Observer:
            async def finish(self, **kwargs):
                calls.append(kwargs)
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()

        owner = LifecycleObserverOwner(Observer())
        caller = asyncio.create_task(owner.finish(status="completed", reason=""))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            if boundary == "task":
                caller.cancel()
            error = await asyncio.wait_for(asyncio.shield(caller), 1)
            assert error.data["error_code"] == "lifecycle_observer.finish_failed"
            assert owner.finished is False
            assert len(owner.run_context.lifecycle_cleanup_tasks) == 1
            assert await owner.finish(status="failed", reason="repeat") is None
            assert len(calls) == 1
        finally:
            release.set()
            await _settle(owner.run_context)
        assert owner.finished is True

    asyncio.run(scenario())


def test_query_engine_nested_producer_retains_running_generator_instead_of_racing_aclose(
    tmp_path, short_cleanup,
):
    async def scenario():
        release, entered = asyncio.Event(), asyncio.Event()
        effects, events = [], []
        owner = RunContext()
        submission = _submission(tmp_path, owner, skill_manager=object())

        async def runner(**kwargs):
            yield AgentEvent.agent_message_completed("accepted", source="model_final")
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            effects.append("runner settled")

        async def consume():
            async for event in QueryEngine(runner=runner).submit(submission):
                events.append(event)

        caller = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(caller), 1)
            done = [event for event in events if event.type == "done"]
            assert len(done) == 1
            assert done[0].data["status"] == "cancelled"
            assert done[0].data["lifecycle_cleanup_pending_count"] > 0
            assert effects == []
            assert submission.session.active_turn is False
            assert submission.session.lifecycle_cleanup_tasks is owner.lifecycle_cleanup_tasks
            # A new empty RunContext must not hide the previous session's owner.
            with pytest.raises(RuntimeError, match="pending lifecycle cleanup"):
                await anext(QueryEngine(runner=runner).submit(QuerySubmission(
                    user_message="next", session=submission.session,
                    runtime=AgentLoopSessionContext(run_context=RunContext()),
                )))
        finally:
            release.set()
            await _settle(owner)
            await submission.session.aclose()
        assert effects == ["runner settled"]

    asyncio.run(scenario())


@pytest.mark.parametrize("host", ["sdk", "rest", "scheduler"])
@pytest.mark.parametrize("phase", ["observe", "finish"])
def test_actual_host_callback_owner_holds_process_claim_after_public_done(
    tmp_path, monkeypatch, short_cleanup, host, phase,
):
    from backend import sdk
    from backend.services import chat_api_service, scheduled_task_runner

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        effects, loans, submissions, events = [], [], [], []
        public_done = asyncio.Event()
        llm = _LLM()
        runtime = AgentRuntime(
            metrics_file=tmp_path / "runtime.jsonl", swarm_store_dir=tmp_path / "swarm",
            enable_lease_heartbeat=False,
        )
        observer_task = None

        class Observer:
            async def hold(self):
                nonlocal observer_task
                observer_task = asyncio.current_task()
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()
                effects.append(await llm.simple_chat([]))

            async def start(self):
                pass

            async def observe(self, event):
                if phase == "observe" and not entered.is_set():
                    await self.hold()

            async def finish(self, **kwargs):
                if phase == "finish":
                    await self.hold()

        async def runner(**kwargs):
            yield AgentEvent.agent_message_completed("accepted answer", source="model_final")
            yield AgentEvent.done(status="completed")

        class ObservedEngine(QueryEngine):
            def __init__(self):
                super().__init__(runner=runner)

            def _setup_query(self, submission):
                owner = submission.runtime.run_context
                assert submission.session.lifecycle_cleanup_tasks is owner.lifecycle_cleanup_tasks
                owner.retain_model = lambda adapter, task: loans.append((adapter, task))
                owner.agent_runtime = runtime
                owner.execution_journal = runtime.execution_journal(f"callback-{host}-{phase}")
                submission.session.lifecycle_observer_factory = lambda **kwargs: Observer()
                submissions.append(submission)
                return super()._setup_query(submission)

            async def submit(self, submission):
                async with aclosing(super().submit(submission)) as stream:
                    async for event in stream:
                        events.append(event)
                        if event.type == "done":
                            public_done.set()
                        yield event

        monkeypatch.setattr(sdk, "QueryEngine", ObservedEngine)
        monkeypatch.setattr(chat_api_service, "default_runtime", lambda: runtime)
        monkeypatch.setattr(scheduled_task_runner, "default_runtime", lambda: runtime)
        monkeypatch.setattr(scheduled_task_runner, "main_worktree_root", lambda path: path.resolve())
        monkeypatch.setattr(scheduled_task_runner, "git_branch_for", lambda path: "main")
        monkeypatch.setattr(chat_api_service, "QueryEngine", ObservedEngine)
        bootstrap = SimpleNamespace(
            create_tool_registry=lambda store, **kwargs: ToolRegistry(),
            create_permission_checker=lambda **kwargs: PermissionChecker(PermissionSettings(), tmp_path),
            create_llm=lambda **kwargs: llm,
        )
        sdk_session = sdk.SDKSession(
            session_id="lifecycle-sdk", llm=llm, tool_registry=ToolRegistry(),
            config=AppConfig(llm=LLMSettings(api_key="unused"), agent=AgentSettings(max_iterations=1)), workspace_root=tmp_path,
            metadata={"conversation_id": "conv-lifecycle-sdk"},
        )
        repository = ConversationRepository(tmp_path / "scheduled-conversations")
        conversation = repository.create_conversation(workspace_root=str(tmp_path))
        monkeypatch.setattr(scheduled_task_runner, "ConversationRepository", lambda: repository)
        conversation_id = conversation.id if host == "scheduler" else f"conv-lifecycle-{host}"

        async def invoke():
            if host == "sdk":
                return [event async for event in sdk_session.query("inspect")]
            if host == "rest":
                return await chat_api_service.run_rest_chat(
                    message="inspect", max_iterations=1, bootstrap=bootstrap,
                    query_engine=ObservedEngine(), workspace_root=tmp_path,
                    conversation_id=conversation_id,
                )
            return await scheduled_task_runner.run_scheduled_task(
                SimpleNamespace(
                    id="schedule", name="audit", prompt="inspect", workspace_root=str(tmp_path),
                    conversation_id=conversation_id, permission_mode="confirm", isolation="workspace",
                ),
                SimpleNamespace(id="scheduled-lifecycle", conversation_id=conversation_id),
                bootstrap=bootstrap,
            )

        caller = asyncio.create_task(invoke())
        closer = None
        try:
            await asyncio.wait_for(entered.wait(), 1)
            if phase == "observe":
                caller.cancel()
                if host == "scheduler":
                    await asyncio.wait_for(public_done.wait(), 1)
                    assert not caller.done()
                else:
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(asyncio.shield(caller), 1)
            elif host == "scheduler":
                await asyncio.wait_for(public_done.wait(), 1)
                assert not caller.done()
                persisted = repository.get_conversation(conversation_id)
                assert persisted.transcript[-1]["role"] == "assistant"
                assert persisted.context_snapshot["scheduled_task"]["lifecycle_cleanup_pending_count"] > 0
            else:
                result = await asyncio.wait_for(asyncio.shield(caller), 1)
                if host != "sdk":
                    assert result["status"] == "completed"
                    assert result["lifecycle_cleanup_pending_count"] > 0
                if host == "scheduler":
                    persisted = repository.get_conversation(conversation_id)
                    assert persisted.transcript[-1]["role"] == "assistant"
                    assert persisted.context_snapshot["scheduled_task"]["lifecycle_cleanup_pending_count"] > 0
            done = [event for event in events if event.type == "done"]
            assert len(done) == 1
            assert done[0].data["lifecycle_cleanup_pending_count"] > 0
            assert any(not receipt["completed"] for receipt in done[0].data["lifecycle_cleanup_receipts"].values())
            assert effects == []
            assert observer_task in {task for adapter, task in loans if adapter is llm}
            owner = submissions[0].runtime.run_context
            assert submissions[0].session.lifecycle_cleanup_tasks is owner.lifecycle_cleanup_tasks
            assert conversation_query_guards().active_claim(conversation_id) is not None
            busy = [event async for event in sdk.query(
                "competing host", metadata={"conversation_id": conversation_id},
            )]
            assert busy[-1].data["reason"] == "conversation_busy"
            if host == "sdk":
                assert sdk_session.lifecycle_cleanup_tasks is owner.lifecycle_cleanup_tasks
                with pytest.raises(RuntimeError):
                    await anext(sdk_session.query("next", metadata={"conversation_id": "different"}))
                with pytest.raises(RuntimeError):
                    sdk_session.fork()
                closer = asyncio.create_task(sdk_session.aclose())
                await asyncio.sleep(0)
                assert not closer.done()
            journal = owner.execution_journal
            latest = [event for event in journal.read_events() if event.payload.get("lifecycle") == "lifecycle_cleanup"]
            assert latest[-1].payload["lifecycle_cleanup_pending_count"] > 0
        finally:
            release.set()
            if submissions:
                await _settle(submissions[0].runtime.run_context)
            if host == "scheduler":
                if phase == "observe":
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(asyncio.shield(caller), 1)
                else:
                    assert (await asyncio.wait_for(asyncio.shield(caller), 1))["status"] == "completed"
            await _claim_released(conversation_id)
            await sdk_session.aclose()
            if closer is not None:
                await closer
            runtime.close(release_lease=True)
        assert effects == ["late model use"]

    asyncio.run(scenario())


def test_real_registry_timeout_late_mutation_keeps_shared_owner_and_other_session_busy(
    tmp_path, monkeypatch, short_cleanup,
):
    async def no_repository_probe(_root, *, context=None):
        return ""

    monkeypatch.setattr("backend.agent.context.build_git_status_context_async", no_repository_probe)

    from backend import sdk

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        captured, loans, submissions = [], [], []
        marker = tmp_path / "late-mutation.txt"
        runtime = AgentRuntime(
            metrics_file=tmp_path / "runtime.jsonl", swarm_store_dir=tmp_path / "swarm",
            enable_lease_heartbeat=False,
        )

        class Tool(BaseTool):
            name = "lifecycle_probe"
            description = "Probe a real non-idempotent registry callback"
            always_load = True
            read_only = False
            mutates_workspace = True
            timeout_seconds = 0.02

            def get_schema(self):
                return ToolSchema(name=self.name, description=self.description,
                                  parameters={"type": "object", "properties": {}})

            async def execute(self, args, context=None):
                captured.append((context, asyncio.current_task()))
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()
                marker.write_text("late side effect", encoding="utf-8")
                return ToolResult(content="late result")

        class ToolLLM(_LLM):
            def __init__(self):
                self.calls = 0

            async def stream_chat(self, messages, tools=None):
                self.calls += 1
                if self.calls == 1:
                    yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=[
                        ToolCallEvent(id="stubborn-call", name="lifecycle_probe", arguments={}),
                    ])
                else:
                    yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Tool timed out; outcome unknown.")
                yield StreamEvent(type=StreamEventType.DONE)

        class CapturingEngine(QueryEngine):
            def _setup_query(self, submission):
                submissions.append(submission)
                submission.runtime.run_context.agent_runtime = runtime
                return super()._setup_query(submission)

        monkeypatch.setattr(sdk, "QueryEngine", CapturingEngine)
        llm = ToolLLM()
        owner = RunContext(retain_model=lambda adapter, task: loans.append((adapter, task)))
        session = sdk.SDKSession(
            session_id="registry-lifecycle", llm=llm, tools=[Tool()], tool_registry=ToolRegistry(),
            config=AppConfig(llm=LLMSettings(api_key="unused"), agent=AgentSettings(max_iterations=2)), workspace_root=tmp_path,
            permission_context=PermissionContext(mode="bypass"),
            metadata={"conversation_id": "conv-real-registry"}, run_context=owner,
        )
        caller = asyncio.create_task(_collect(session.query("execute probe")))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            events = await asyncio.wait_for(asyncio.shield(caller), 2)
            done = [event for event in events if event.type == "done"]
            assert len(done) == 1
            assert done[0].data["lifecycle_cleanup_pending_count"] > 0
            assert not marker.exists()
            tool_context, child = captured[0]
            assert tool_context.pending_cleanup_tasks is owner.lifecycle_cleanup_tasks
            assert session.lifecycle_cleanup_tasks is owner.lifecycle_cleanup_tasks
            assert submissions[0].runtime.metadata["_tool_execution_context"].pending_cleanup_tasks is owner.lifecycle_cleanup_tasks
            assert child in owner.lifecycle_cleanup_tasks and not child.done()
            assert child in {task for adapter, task in loans if adapter is llm}
            assert tool_context.cleanup_receipts["stubborn-call"]["pending"] > 0
            assert conversation_query_guards().active_claim("conv-real-registry") is not None
            other = await _collect(sdk.query("competing", metadata={"conversation_id": "conv-real-registry"}))
            assert other[-1].data["reason"] == "conversation_busy"
            with pytest.raises(RuntimeError, match="pending lifecycle cleanup"):
                await anext(QueryEngine().submit(QuerySubmission(
                    user_message="new query", session=submissions[0].session,
                    runtime=AgentLoopSessionContext(run_context=RunContext()),
                )))
        finally:
            release.set()
            await _settle(owner)
            await _claim_released("conv-real-registry")
            await session.aclose()
            runtime.close(release_lease=True)
        assert marker.read_text("utf-8") == "late side effect"
        assert tool_context.cleanup_receipts["stubborn-call"]["completed"] is True

    asyncio.run(scenario())


async def _collect(stream):
    async with aclosing(stream):
        return [event async for event in stream]


def test_captured_old_adapter_retains_existing_lease_when_selection_has_changed(tmp_path, short_cleanup):
    from backend.agent.model_execution import ModelExecutionSnapshot
    from backend.ws.agent_runner import _clear_session_llm_cache, _lease_session_llm_for_task

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        class Adapter(_LLM):
            def __init__(self):
                self.closed = False
                self.calls = 0

            async def simple_chat(self, messages, **kwargs):
                assert not self.closed
                self.calls += 1
                return "captured A completed"

            async def aclose(self):
                self.closed = True

        old, replacement = Adapter(), Adapter()
        host = SimpleNamespace(_llm_adapter_cache={"old": old, "replacement": replacement})
        snapshot = ModelExecutionSnapshot.capture(AppConfig(llm=LLMSettings(api_key="unused")), replacement)
        owner = RunContext(
            active_model_execution=snapshot, model_execution=snapshot,
            retain_model=lambda adapter, task: _lease_session_llm_for_task(host, adapter, task),
        )

        async def borrower():
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return await old.simple_chat([])

        task = asyncio.create_task(borrower())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await owner.drain_lifecycle_task(task, timeout=0.01, label="captured A", llm=old)
            _clear_session_llm_cache(host)
            await asyncio.sleep(0)
            assert not old.closed
            assert replacement.closed
            assert host._retired_llm_adapters[id(old)] is old
            assert task in host._llm_adapter_leases[id(old)]
            assert id(replacement) not in host._llm_adapter_leases
        finally:
            release.set()
            await _settle(owner)
            if host._llm_close_tasks:
                await asyncio.gather(*host._llm_close_tasks)
        assert task.result() == "captured A completed"
        assert old.calls == 1 and old.closed

    asyncio.run(scenario())


def test_receipt_secret_is_sanitized_once_at_public_and_durable_exit(tmp_path):
    import json
    from backend.agent.execution_journal import ExecutionJournal
    from backend.agent.query_journal import QueryJournalRecorder
    from backend.agent.query_terminal import QueryTerminalTransaction

    async def scenario():
        secret = "sk-fake-lifecycle-private-key"
        owner = RunContext()
        gate = asyncio.Event()

        async def callback():
            await gate.wait()
            raise RuntimeError(f"HTTP 401 authorization failed; Bearer {secret}; token=private-token")

        task = asyncio.create_task(callback())
        owner.retain_lifecycle_task(task, label="auth callback")
        gate.set()
        await _settle(owner)
        assert secret in next(iter(owner.lifecycle_cleanup_receipts.values()))["error"]
        journal = ExecutionJournal("private-receipt", base_dir=tmp_path / "journal")
        recorder = QueryJournalRecorder(
            journal=journal, metadata={"run_id": "private-run"}, state=AgentState(user_message="inspect"),
            context_builder=ContextBuilder(), turn_kernel=None, conversation_id="private-conversation",
        )
        terminal = QueryTerminalTransaction(
            turn_ctx=SimpleNamespace(run_context=owner, metadata={"run_id": "private-run"}), journal=recorder,
        )
        done = AgentEvent.done(status="failed")
        fact = terminal.lifecycle_cleanup_event(done)
        assert terminal.record_post_commit_event(fact) is None
        for exported in (done.data, journal.read_events()[-1].payload):
            encoded = json.dumps(exported, ensure_ascii=False)
            assert secret not in encoded and "private-token" not in encoded
            receipt = next(iter(exported["lifecycle_cleanup_receipts"].values()))
            assert receipt["completed"] and receipt["pending"] == 0
            assert "HTTP 401" in receipt["error"]

    asyncio.run(scenario())


def test_agent_session_command_shutdown_tracks_late_registered_child_not_set_snapshot(tmp_path):
    async def scenario():
        owner = RunContext()
        first_release, child_release, child_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        shutdowns = []

        class Commands:
            async def shutdown(self):
                shutdowns.append("closed")

        async def late_child():
            child_entered.set()
            await child_release.wait()

        async def first_callback():
            await first_release.wait()
            owner.retain_lifecycle_task(asyncio.create_task(late_child()), label="late child")

        first = asyncio.create_task(first_callback())
        owner.retain_lifecycle_task(first, label="first callback")
        session = _submission(tmp_path, owner).session
        session.lifecycle_cleanup_tasks = owner.lifecycle_cleanup_tasks
        session._owned_commands = Commands()
        await session.aclose()
        try:
            first_release.set()
            await asyncio.wait_for(child_entered.wait(), 1)
            await asyncio.sleep(0)
            assert shutdowns == []
            await session.aclose()
            assert len({task for task in owner.lifecycle_cleanup_tasks if not task.done()}) == 2
        finally:
            child_release.set()
            await _settle(owner)
        assert shutdowns == ["closed"]

    asyncio.run(scenario())
