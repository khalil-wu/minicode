"""Atomic Source score/authorization/coverage publication on the owning loop."""
from __future__ import annotations
from dataclasses import dataclass
from collections import deque
from typing import Any


@dataclass(frozen=True)
class CachedScore:
    lag: int
    has_unscored_failure: bool
    js_executions: int
    oversized: bool
    action_risk: float | None
    authorization: Any


class GuardianScoreProgress:
    def __init__(self):
        self.latest_tool_call = 0
        self.latest_scored_tool_call = 0
        self.latest_failed_tool_call = 0
        self.js_executions = 0
        self.oversized = set()
        self.score = None
        self.sampled_at = None
        self.authorization = None
        self.starts = deque(maxlen=256)

    def observe(self, call_id, *, wrapper=False, parent_wrapper_call_id=None):
        self.latest_tool_call += 1
        parent = next((index for known_id, index, is_wrapper, _ in reversed(self.starts)
            if is_wrapper and known_id == parent_wrapper_call_id), None) if parent_wrapper_call_id is not None else None
        self.starts.append((call_id, self.latest_tool_call, wrapper, parent))
        return self.latest_tool_call

    def invalidate(self, index):
        self.latest_failed_tool_call = max(self.latest_failed_tool_call, index)

    def mark_oversized(self, call_id, index):
        self.oversized.add(call_id)
        self.invalidate(index)

    def finish(self, call_id):
        self.oversized.discard(call_id)

    def publish(self, score, authorization, index, sampled_at):
        if self.sampled_at is not None and sampled_at <= self.sampled_at:
            return False
        self.score, self.authorization, self.sampled_at = score, authorization, sampled_at
        self.latest_scored_tool_call = max(self.latest_scored_tool_call, index)
        return True

    def fail_closed(self, sampled_at):
        if self.sampled_at is None or sampled_at >= self.sampled_at:
            self.score, self.sampled_at = 1., sampled_at

    def inspect(self, call_id=None):
        parent = next((parent for known_id, _, _, parent in reversed(self.starts) if known_id == call_id), None)
        discount = int(parent is not None and parent > self.latest_scored_tool_call)
        return CachedScore(max(0, self.latest_tool_call - self.latest_scored_tool_call - discount),
            self.latest_failed_tool_call > self.latest_scored_tool_call, self.js_executions, call_id in self.oversized,
            self.score, self.authorization)
