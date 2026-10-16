"""Source process.rs bounded replay history and independent live receivers."""
from __future__ import annotations

import asyncio
from collections import deque
from weakref import WeakSet

from .errors import ExecutorEventsLagged

EVENT_CAPACITY = 256
RETAINED_EVENT_BYTES = 1024 * 1024


def event_bytes(event: dict) -> int:
    if event["method"] == "process/output":
        return len(event["chunk"])
    if event["method"] == "process/failed":
        return len(event["error"].encode("utf-8"))
    return 0


class ProcessEventReceiver:
    def __init__(self, replay):
        self.replay = deque(replay)
        self.live = deque()
        self.lost = 0
        self.changed = asyncio.Event()

    def publish(self, event: dict) -> None:
        if len(self.live) == EVENT_CAPACITY:
            self.live.popleft()
            self.lost += 1
        self.live.append(event)
        self.changed.set()

    async def recv(self) -> dict:
        while True:
            if self.replay:
                return self.replay.popleft()
            if self.lost:
                lost, self.lost = self.lost, 0
                raise ExecutorEventsLagged(lost)
            if self.live:
                return self.live.popleft()
            self.changed.clear()
            await self.changed.wait()


class ProcessEventLog:
    def __init__(self):
        self.history = deque()
        self.retained_bytes = 0
        self.receivers = WeakSet()

    def publish(self, event: dict) -> None:
        self.history.append(event)
        self.retained_bytes += event_bytes(event)
        while len(self.history) > EVENT_CAPACITY or self.retained_bytes > RETAINED_EVENT_BYTES:
            self.retained_bytes -= event_bytes(self.history.popleft())
        for receiver in self.receivers:
            receiver.publish(event)

    def subscribe(self) -> ProcessEventReceiver:
        receiver = ProcessEventReceiver(self.history)
        self.receivers.add(receiver)
        return receiver
