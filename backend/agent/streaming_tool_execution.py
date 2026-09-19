"""Schedule committed provider items through the existing tool executor."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from copy import deepcopy
from dataclasses import asdict
from typing import Any

from backend.agent.loop_runtime_helpers import epoch_ms
from backend.agent.provider_stream_runtime import ProviderStreamResult
from backend.agent.runtime_spans import runtime_span_from_tool_context
from backend.agent.tool_batch_execution import _parallel_tool_concurrency, batch_tool_calls
from backend.agent.tool_batch_runner import ToolBatchRunner
from backend.agent.tool_transition import prepare_tool_transition
from backend.async_cleanup import to_thread_cancel_safe


class StreamingToolExecution:
    """One iteration's read/write admission gate and event forwarding scope."""

    def __init__(self, owner: Any, iteration_id: str) -> None:
        self.owner = owner
        self.iteration_id = iteration_id
        self.calls: dict[str, Any] = {}
        self.history_message = None
        self.tasks: list[asyncio.Task] = []
        self._reads: list[asyncio.Task] = []
        self._write: asyncio.Task | None = None
        self._slots = asyncio.Semaphore(_parallel_tool_concurrency(1_000_000))
        self._events: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._closing = False
        self._journal_message = None
        maximum = owner.settings.max_tool_calls
        self._remaining = max(0, maximum - (len(owner.agent_state.tool_calls) - owner.turn_start_tool_call_count)) if maximum > 0 else None

    @property
    def started(self) -> bool:
        return bool(self.calls)

    async def submit(self, calls: list[Any], stream_state: Any, stream_text: Any, tracker: Any) -> None:
        for call in calls:
            previous = self.calls.get(call.id)
            if previous is not None:
                if previous.name != call.name or previous.arguments != call.arguments:
                    raise ValueError(f"Provider changed committed tool call {call.id}")
                continue
            owner = self.owner
            prepared = prepare_tool_transition(state=owner.agent_state, tool_calls=[call], tool_registry=owner.tool_registry, tool_context=owner.tool_context).tool_calls[0]
            self.calls[call.id] = deepcopy(call)
            stream_state.committed_tool_ids.add(call.id)
            self.history_message = owner.context_builder.append_assistant_tool_calls(
                [prepared], content=stream_text.full_text, phase=stream_state.response_phase or "commentary",
                provider_items=stream_state.response_items, message=self.history_message,
            )
            await self._record_provider_item()
            owner.chain.record_tool_call()
            concurrent = batch_tool_calls([prepared], owner.tool_registry)[0][0]
            dependencies = ([self._write] if self._write is not None else [])
            if not concurrent:
                dependencies += self._reads
            allowed = self._remaining is None or len(self.calls) <= self._remaining
            task = asyncio.create_task(self._run(prepared, dependencies, tracker, allowed, epoch_ms()), name=f"tool:{prepared.id}")
            self.tasks.append(task)
            if concurrent:
                self._reads.append(task)
            else:
                self._write = task
                self._reads = []

    async def _publish(self, event: Any) -> None:
        if not self._closing:
            acknowledged = asyncio.Event()
            await self._events.put((event, acknowledged))
            # QueryJournalRecorder processes the event before the consumer
            # resumes us. Tool execution must not overtake durable tool_use.
            await acknowledged.wait()

    async def _record_provider_item(self) -> None:
        journal = self.owner.tool_context.run_context.execution_journal
        if journal is not None and self.history_message is not None:
            message = asdict(self.history_message)
            if message != self._journal_message:
                await to_thread_cancel_safe(journal.append_lifecycle, "provider_item_committed", {
                    "iteration_id": self.iteration_id, "message": message,
                })
                self._journal_message = message

    async def _run(self, call: Any, dependencies: list[asyncio.Task], tracker: Any, allowed: bool, ready_at: int) -> None:
        owner = self.owner
        span_id = f"tool-dispatch:{self.iteration_id}:{call.id}"
        await self._publish(runtime_span_from_tool_context(
            "tool.queued", span_id=span_id, tool_ctx=owner.tool_context,
            iteration_id=self.iteration_id, tool_call_id=call.id, tool_name=call.name,
            started_at=ready_at, ui_visible=False,
        ))
        if dependencies:
            # Waiting for a predecessor does not own its cancellation. The
            # iteration scope cancels each running tool exactly once.
            await asyncio.gather(*(asyncio.shield(task) for task in dependencies))
        async with self._slots:
            if owner.tool_context.cancel_event is not None and owner.tool_context.cancel_event.is_set():
                raise asyncio.CancelledError
            owner.turn_kernel.refresh_live_permission_context()
            tracker.mark_yielded(call.id)
            started_at = epoch_ms()
            await self._publish(runtime_span_from_tool_context(
                "tool.queued", span_id=span_id, tool_ctx=owner.tool_context,
                iteration_id=self.iteration_id, tool_call_id=call.id, tool_name=call.name,
                started_at=ready_at, ended_at=started_at, duration_ms=started_at - ready_at,
                status="completed", ui_visible=False, data={"queue_wait_ms": started_at - ready_at},
            ))
            runner = ToolBatchRunner(
                ctx=owner.context_builder, state=owner.agent_state, tool_registry=owner.tool_registry,
                permission_checker=owner.permission_checker, approval_handler=owner.approval_handler,
                skill_manager=owner.skill_manager, permission_context=owner.tool_context.permission,
                tool_ctx=owner.tool_context,
            )
            async with aclosing(runner.run([call], prepared_tool_calls=[call], execution_limit=1 if allowed else 0)) as events:
                async for event in events:
                    await self._publish(event)

    async def _pump_provider(self, provider_events: Any):
        # Keep one task/context for the entire async generator. Provider
        # adapters bind ContextVars across yields and must close in that context.
        result = None
        async with aclosing(provider_events) as events:
            async for event in events:
                if isinstance(event, ProviderStreamResult):
                    result = event
                else:
                    await self._publish(event)
        return result

    async def project(self, provider_events: Any):
        """Forward tool progress while the provider is still producing its tail."""
        provider = asyncio.create_task(self._pump_provider(provider_events))
        next_event = asyncio.create_task(self._events.get())
        result = None
        failure = None
        stopping = False
        try:
            while True:
                active = [task for task in self.tasks if not task.done()]
                if not stopping:
                    for task in [provider, *self.tasks]:
                        if task.done():
                            if task.cancelled():
                                failure = asyncio.CancelledError()
                                break
                            if task.exception() is not None:
                                failure = task.exception()
                                break
                    if provider.done() and failure is None:
                        result = provider.result()
                    if failure is not None or (result is not None and result.action == "terminate"):
                        stopping = True
                        for task in [provider, *active]:
                            if not task.done():
                                task.cancel()
                if provider.done() and not active:
                    if next_event.done():
                        event, acknowledged = next_event.result()
                        yield event
                        acknowledged.set()
                    else:
                        next_event.cancel()
                        await asyncio.gather(next_event, return_exceptions=True)
                    while not self._events.empty():
                        event, acknowledged = self._events.get_nowait()
                        yield event
                        acknowledged.set()
                    break
                waiters = {next_event, *active}
                if not provider.done():
                    waiters.add(provider)
                try:
                    done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                except asyncio.CancelledError as exc:
                    failure = exc
                    continue
                if next_event in done:
                    event, acknowledged = next_event.result()
                    yield event
                    acknowledged.set()
                    next_event = asyncio.create_task(self._events.get())
            if failure is not None:
                raise failure
            if result is not None:
                if self.history_message is not None:
                    self.owner.context_builder.append_assistant_tool_calls(
                        [], content=result.stream_text.full_text,
                        phase=result.response_phase or "commentary",
                        provider_items=result.stream_state.response_items, message=self.history_message,
                    )
                    await self._record_provider_item()
                if result.stream_text.agent_message_started and result.stream_text.active_agent_message_source == "commentary":
                    completed = result.stream_text.complete_active_agent_message(
                        result.stream_text.active_agent_message_text, source="commentary",
                        status="partial" if result.action == "terminate" else "completed",
                    )
                    if completed is not None:
                        yield completed
                yield result
        finally:
            self._closing = True
            tasks = [*self.tasks, next_event, provider]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.history_message is not None:
                self.owner.context_builder.settle_streamed_tool_message(self.history_message)
