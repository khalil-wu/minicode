"""One query event stream for model output and acknowledged nested tool facts."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import asdict, dataclass

from backend.agent.message import AgentEvent
from backend.agent.query_journal import QueryJournalRecorder
from backend.async_cleanup import to_thread_cancel_safe


async def publish_tool_event(run_context, event, source):
    event.data["call_source"] = asdict(source)
    if event.type in {"done", "error", "agent.run.started", "agent.run.completed", "agent.terminal.intent",
                      "item.started", "item.completed", "agent_message.delta", "text_chunk", "thinking", "thinking_delta", "context_compacted"}:
        if run_context.execution_journal is not None:
            await to_thread_cancel_safe(run_context.execution_journal.append_lifecycle,
                "nested_tool_event", {"event_type": event.type, **event.data})
        return
    await run_context.publish_nested_event(event)


@dataclass
class _QueuedEvent:
    event: AgentEvent
    acknowledged: asyncio.Future[None]


@dataclass
class CallbackCompleted:
    result: AgentEvent | None


class NestedToolEvents:
    def __init__(self, journal: QueryJournalRecorder):
        self.journal = journal
        self._queue: asyncio.Queue[_QueuedEvent | BaseException | None] = asyncio.Queue()
        self._closed = False

    async def publish(self, event: AgentEvent) -> None:
        # Recording stays available during cleanup, after the consumer closes.
        # A tool must not overtake its durable claim even if its UI is slow.
        await to_thread_cancel_safe(self.journal.record_event, event)
        if self._closed:
            raise asyncio.CancelledError
        pending = _QueuedEvent(event, asyncio.get_running_loop().create_future())
        await self._queue.put(pending)
        await pending.acknowledged
        if self._closed:
            raise asyncio.CancelledError

    async def run_callback(self, callback):
        """Serve nested calls while the triggering observer is still awaiting.

        This keeps approvals and execution claims observable before the
        callback finishes, with the same acknowledgement contract as model
        tools. It also prevents an observer from waiting on its own consumer.
        """
        task = asyncio.ensure_future(callback)
        try:
            while not task.done():
                next_event = asyncio.create_task(self._queue.get())
                try:
                    await asyncio.wait({task, next_event}, return_when=asyncio.FIRST_COMPLETED)
                    if next_event.done():
                        queued = next_event.result()
                        if queued is None:
                            raise asyncio.CancelledError
                        if isinstance(queued, BaseException):
                            raise queued
                        try:
                            yield queued.event
                        except BaseException:
                            task.cancel()
                            raise
                        finally:
                            if not queued.acknowledged.done():
                                queued.acknowledged.set_result(None)
                finally:
                    next_event.cancel()
                    await asyncio.gather(next_event, return_exceptions=True)
            yield CallbackCompleted(task.result())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def stream(self, runner: AsyncIterator[AgentEvent]) -> AsyncIterator[AgentEvent]:
        async def produce():
            try:
                # The generator is entered, resumed and closed in one task so
                # provider/extension ContextVars never cross task ownership.
                async with aclosing(runner):
                    async for event in runner:
                        await self.publish(event)
            except BaseException as error:
                await self._queue.put(error)
            finally:
                await self._queue.put(None)

        producer = asyncio.create_task(produce(), name="query-tool-events")
        try:
            while True:
                queued = await self._queue.get()
                if queued is None:
                    break
                if isinstance(queued, BaseException):
                    raise queued
                try:
                    yield queued.event
                finally:
                    if not queued.acknowledged.done():
                        queued.acknowledged.set_result(None)
        finally:
            self._closed = True
            producer.cancel()
            while not self._queue.empty():
                queued = self._queue.get_nowait()
                if isinstance(queued, _QueuedEvent):
                    queued.acknowledged.cancel()
            await asyncio.gather(producer, return_exceptions=True)
