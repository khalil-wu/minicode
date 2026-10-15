"""Source single-owner retained conversations with ordered reservation and commit."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from backend.agent.guardian.async_request import prepare_retained
from backend.agent.guardian.async_sampler import GuardianSamplingError


@dataclass
class ConversationRequest:
    evidence: object
    instructions: str
    checkpoint: object
    context: object
    authorization: object
    reset_token_limit: int
    ready: asyncio.Future


class Reservation:
    def __init__(self, backend, future):
        self.backend = backend
        self.future = future

    def submit(self, request):
        if self.future.cancelled():
            self.backend._superseded(request)
        else:
            self.future.set_result(request)

    def abandon(self):
        if not self.future.done():
            self.future.cancel()


class ConversationBackend:
    def __init__(self, sampler, is_current):
        self.sampler = sampler
        self.is_current = is_current
        self.queue = asyncio.Queue()
        self.outstanding = 0
        self.valid = True
        self.history = []
        self.cursor = None
        self.reuse_key = None
        self.completed_reviews = 0
        self.audit = []
        self.worker = asyncio.create_task(self._run())

    def reserve(self):
        if not self.valid:
            raise GuardianSamplingError("superseded", "Guardian conversation generation has been replaced")
        if self.outstanding == 16:
            raise GuardianSamplingError("queue_full", "Guardian conversation queue is full")
        future = asyncio.get_running_loop().create_future()
        self.outstanding += 1
        self.queue.put_nowait(future)
        return Reservation(self, future)

    def invalidate(self):
        if self.valid:
            self.valid = False
            self.queue.put_nowait(None)

    async def _run(self):
        request = None
        try:
            while (reservation := await self.queue.get()) is not None:
                try:
                    try:
                        request = await reservation
                    except asyncio.CancelledError:
                        if asyncio.current_task().cancelling():
                            raise
                        continue
                    if not self.valid or not self.is_current(request.authorization):
                        self.history, self.cursor, self.reuse_key = [], None, None
                        self._superseded(request)
                        continue
                    key = request.authorization, request.instructions, request.checkpoint
                    if key != self.reuse_key:
                        self.history, self.cursor = [], None
                    prepared = prepare_retained(request.evidence, self.history, self.cursor,
                        sampler=self.sampler, instructions=request.instructions, checkpoint=request.checkpoint,
                        reset_token_limit=request.reset_token_limit)
                    # An in-flight prepared request exclusively owns this prefix.
                    self.history, self.cursor, self.reuse_key = [], None, None
                    history = await self.sampler.sample_retained(prepared, ready=request.ready,
                        instructions=request.instructions, context=request.context, reasoning_effort=request.evidence.config.reasoning_effort)
                    if history is not None and self.valid and self.is_current(request.authorization):
                        self.history, self.cursor, self.reuse_key = history, prepared.cursor, key
                        self.completed_reviews += 1
                        self.audit.append({"status": "committed", "input_tokens": prepared.input_tokens,
                            "existing_context_tokens": prepared.existing_context_tokens,
                            "transcript_entry_count": self.cursor.transcript_entry_count})
                    else:
                        self.audit.append({"status": "not_retained"})
                except GuardianSamplingError as error:
                    self.history, self.cursor, self.reuse_key = [], None, None
                    if not request.ready.done():
                        request.ready.set_exception(error)
                    self.audit.append({"status": "not_retained", "reason": error.kind})
                except BaseException as error:
                    if request is not None and not request.ready.done():
                        request.ready.set_exception(error)
                    raise
                finally:
                    self.outstanding -= 1
                    request = None
                    reservation = None
                    prepared = None
                    history = None
        finally:
            self.valid = False
            self.history, self.cursor, self.reuse_key = [], None, None
            if request is not None:
                self._superseded(request)
            while not self.queue.empty():
                reservation = self.queue.get_nowait()
                if reservation is not None:
                    if reservation.done() and not reservation.cancelled():
                        self._superseded(reservation.result())
                    else:
                        reservation.cancel()
                    self.outstanding -= 1

    @staticmethod
    def _superseded(request):
        if not request.ready.done():
            request.ready.set_exception(GuardianSamplingError("superseded", "Guardian conversation request was superseded"))

    async def aclose(self):
        self.invalidate()
        self.worker.cancel()
        await asyncio.gather(self.worker, return_exceptions=True)
