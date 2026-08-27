"""Conversation history container owned by :class:`ContextBuilder`.

Extracted from ``backend/agent/context.py`` so the ordered message list and
its derived accounting (frozen provider prefix, durable timestamps, token
cache) live in one independently testable unit. ``ContextBuilder`` composes
this store and delegates mutation through it; business logic (compaction,
budgets, media recovery) stays in the builder but operates on
``history.messages``. This mirrors how Codex keeps ``ContextManager`` and Pi
keeps session storage as separate components.
"""

from __future__ import annotations

import time
from copy import deepcopy
from typing import Any, Callable

from backend.llm.base import LLMMessage, ToolCallEvent

MessageEstimator = Callable[[LLMMessage, Any | None], int]


def _estimate_content_tokens(content: Any) -> int:
    """Estimate tokens with the provider-neutral ``chars / 4`` heuristic."""
    if isinstance(content, str):
        return (len(content) + 3) // 4
    if content is None:
        return 0
    return (len(str(content)) + 3) // 4


def estimate_message_tokens(
    content: Any,
    tool_calls: list[ToolCallEvent] | None = None,
) -> int:
    """Rough per-message token estimate used by the history token cache."""
    return _estimate_content_tokens(content) + (len(tool_calls or []) * 20)


def repair_tool_messages(
    messages: list[LLMMessage],
) -> tuple[list[LLMMessage], int, int]:
    """Pair assistant tool_calls with their tool results.

    Tool result messages are only legal as replies to the immediately
    preceding assistant message's tool_calls. Interrupted streams and older
    snapshots can leave either dangling assistant tool_calls or orphan tool
    messages. Returns ``(repaired, inserted, dropped)`` where ``inserted``
    counts synthesized placeholders for dangling calls and ``dropped`` counts
    orphan tool messages removed.

    This is the single implementation used both for the live history
    (``ConversationHistory.repair``) and for budget-filtered projections
    (``repair_projected_messages``).
    """

    def make_placeholder(call_id: str, tool_name: str) -> LLMMessage:
        return LLMMessage(
            role="tool",
            content=(
                f"[Tool call '{tool_name}' did not complete. "
                "Do not retry the same call; use the information you already have or try a different approach.]"
            ),
            name=tool_name,
            tool_call_id=call_id,
            is_error=True,
        )

    repaired: list[LLMMessage] = []
    pending_ids: dict[str, str] = {}
    pending_order: list[str] = []
    inserted = 0
    dropped = 0

    def flush_pending() -> None:
        nonlocal inserted
        if not pending_order:
            return
        for call_id in list(pending_order):
            repaired.append(make_placeholder(call_id, pending_ids.get(call_id, "unknown")))
            inserted += 1
        pending_ids.clear()
        pending_order.clear()

    for message in messages:
        if message.role == "assistant":
            flush_pending()
            repaired.append(message)
            for tool_call in message.tool_calls or []:
                pending_ids[tool_call.id] = tool_call.name
                pending_order.append(tool_call.id)
            continue
        if message.role == "tool":
            call_id = str(message.tool_call_id or "").strip()
            if call_id and call_id in pending_ids:
                repaired.append(message)
                pending_ids.pop(call_id, None)
                pending_order = [pending for pending in pending_order if pending != call_id]
            else:
                dropped += 1
            continue
        flush_pending()
        repaired.append(message)

    flush_pending()
    return repaired, inserted, dropped


def group_messages(messages: list[LLMMessage]) -> list[list[LLMMessage]]:
    """Group each assistant tool_calls message with its adjacent tool results."""
    groups: list[list[LLMMessage]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        group = [message]
        if message.role == "assistant" and message.tool_calls:
            call_ids = {call.id for call in message.tool_calls if call.id}
            cursor = index + 1
            while cursor < len(messages):
                candidate = messages[cursor]
                if candidate.role != "tool" or (call_ids and candidate.tool_call_id not in call_ids):
                    break
                group.append(candidate)
                cursor += 1
            index = cursor
        else:
            index += 1
        groups.append(group)
    return groups


def group_raw_messages(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Same grouping as :func:`group_messages` for raw snapshot dictionaries."""
    groups: list[list[dict[str, Any]]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        group = [message]
        tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
        if (
            message.get("role") == "assistant"
            and isinstance(tool_calls, list)
            and tool_calls
        ):
            call_ids = {
                str(call.get("id") or "")
                for call in tool_calls
                if isinstance(call, dict) and str(call.get("id") or "")
            }
            cursor = index + 1
            while cursor < len(messages):
                candidate = messages[cursor]
                if candidate.get("role") != "tool" or (
                    call_ids
                    and str(candidate.get("tool_call_id") or "") not in call_ids
                ):
                    break
                group.append(candidate)
                cursor += 1
            index = cursor
        else:
            index += 1
        groups.append(group)
    return groups


class ConversationHistory:
    """Owns the ordered message list and its derived accounting.

    ``ContextBuilder`` exposes this object through property delegates
    (``_history``, ``_history_frozen_count``, ...) so existing builder logic
    keeps working while every mutation funnels through this store.
    """

    def __init__(self, estimator: MessageEstimator | None = None) -> None:
        self.messages: list[LLMMessage] = []
        # Number of transcript messages whose exact bytes have already been
        # handed to a provider. Once a message is in this prefix, runtime
        # refresh/cleanup must never rewrite it.
        self.frozen_count = 0
        # Older snapshots have no sent-boundary marker. Treat their durable
        # transcript as already sent and carry that conservative assumption
        # through partial hydration.
        self.frozen_metadata_present = False
        self.pending_hydration_frozen_prefix_count = 0
        self.last_message_timestamp_ms = 0
        self._estimator = estimator or (
            lambda message, raw_content: estimate_message_tokens(
                raw_content if raw_content is not None else message.content,
                message.tool_calls,
            )
        )
        self._token_estimates: list[int] = []
        self._tokens_total = 0

    # ── queries ──

    def __len__(self) -> int:
        return len(self.messages)

    @property
    def token_estimates(self) -> list[int]:
        return self._token_estimates

    @property
    def tokens_total(self) -> int:
        return self._tokens_total

    # ── mutation ──

    def append(self, message: LLMMessage, *, raw_content: Any | None = None) -> None:
        """Append one message with a durable timestamp and token estimate."""
        now_ms = max(1, int(time.time() * 1000))
        if message.timestamp_ms is None:
            message.timestamp_ms = max(now_ms, self.last_message_timestamp_ms + 1)
        else:
            try:
                message.timestamp_ms = max(1, int(message.timestamp_ms))
            except (TypeError, ValueError):
                message.timestamp_ms = max(1, self.last_message_timestamp_ms + 1)
        self.last_message_timestamp_ms = max(
            self.last_message_timestamp_ms,
            int(message.timestamp_ms or 1),
        )
        self.messages.append(message)
        estimate = int(self._estimator(message, raw_content))
        self._token_estimates.append(estimate)
        self._tokens_total += estimate

    def prepend(self, messages: list[LLMMessage]) -> None:
        """Prepend hydrated prefix messages, adjusting the frozen boundary."""
        if not messages:
            # A completed/empty hydration callback means no older prefix will
            # be prepended. Do not leave an absolute pending boundary around
            # for a later unrelated append or skill restore.
            self.pending_hydration_frozen_prefix_count = 0
            return
        self.messages = messages + self.messages
        self.frozen_count = min(
            len(self.messages),
            max(0, int(self.pending_hydration_frozen_prefix_count))
            + max(0, int(self.frozen_count)),
        )
        self.pending_hydration_frozen_prefix_count = 0
        self.ensure_timestamps()
        self.rebuild_token_cache()

    def replace_all(self, messages: list[LLMMessage]) -> None:
        self.messages = messages
        self.rebuild_token_cache()

    def clear(self) -> None:
        self.messages.clear()
        self.frozen_count = 0
        self.frozen_metadata_present = False
        self.pending_hydration_frozen_prefix_count = 0
        self.last_message_timestamp_ms = 0
        self._token_estimates.clear()
        self._tokens_total = 0

    # ── derived state ──

    def ensure_timestamps(self) -> None:
        """Assign durable timestamps to messages inserted outside the append API."""
        current = max(
            (int(message.timestamp_ms or 0) for message in self.messages),
            default=self.last_message_timestamp_ms,
        )
        for message in self.messages:
            if message.timestamp_ms is None:
                current = max(current + 1, 1)
                message.timestamp_ms = current
            else:
                try:
                    message.timestamp_ms = max(1, int(message.timestamp_ms))
                except (TypeError, ValueError):
                    current = max(current + 1, 1)
                    message.timestamp_ms = current
            current = max(current, int(message.timestamp_ms or 1))
        self.last_message_timestamp_ms = max(self.last_message_timestamp_ms, current)

    def rebuild_token_cache(self) -> None:
        self._token_estimates = [int(self._estimator(message, message.content)) for message in self.messages]
        self._tokens_total = sum(self._token_estimates)

    def repair(self) -> int:
        """Repair dangling/orphan tool messages in place.

        Returns the number of placeholders inserted. When anything changed,
        the frozen provider prefix is reset because the serialized prompt
        changed.
        """
        repaired, inserted, dropped = repair_tool_messages(self.messages)
        if inserted or dropped:
            self.messages = repaired
            self.frozen_count = 0
            self.rebuild_token_cache()
        return inserted

    def groups(self) -> list[list[LLMMessage]]:
        return group_messages(self.messages)

    def clone(self) -> "ConversationHistory":
        """Return an independent copy for branch-style agent runs.

        The estimator callback is shared by reference (it is a pure function),
        never deep-copied, so cloning never drags the owning builder along.
        """
        cloned = ConversationHistory(estimator=self._estimator)
        cloned.messages = deepcopy(self.messages)
        cloned.frozen_count = self.frozen_count
        cloned.frozen_metadata_present = self.frozen_metadata_present
        cloned.pending_hydration_frozen_prefix_count = self.pending_hydration_frozen_prefix_count
        cloned.last_message_timestamp_ms = self.last_message_timestamp_ms
        cloned._token_estimates = list(self._token_estimates)
        cloned._tokens_total = self._tokens_total
        return cloned
