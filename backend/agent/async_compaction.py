"""Session-owned speculative native compaction and prefix adoption."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
import logging
from typing import Any
from uuid import uuid4

from backend.async_cleanup import to_thread_cancel_safe
from backend.llm.base import LLMTurnContext

logger = logging.getLogger(__name__)


def threshold_percent(config) -> int:
    effective = config.config_layer_stack.effective_config() if config.config_layer_stack is not None else {}
    value = effective.get("features", {}).get("async_compaction", {})
    threshold = value.get("model_async_compact_threshold_percent", 65) if isinstance(value, dict) else 65
    if type(threshold) is not int or not 1 <= threshold <= 100:
        raise ValueError("features.async_compaction.model_async_compact_threshold_percent must be between 1 and 100")
    return threshold


def enabled(context, execution) -> bool:
    return (execution is not None and execution.config.feature_flags.enabled("async_compaction")
        and not context.token_budget_enabled and not context.auto_compaction_disabled
        and execution.llm.remote_compaction_v2)


@dataclass(eq=False)
class CompactionCandidate:
    history: Any
    generation: int
    prefix: list = field(repr=False)
    identity: tuple = field(repr=False)
    adapter: Any = field(repr=False)
    task: asyncio.Task | None = field(default=None, repr=False)
    replacement: Any = None
    error: str = ""

    def matches(self, context, execution) -> bool:
        if execution is None or execution.llm.compaction_identity() != self.identity:
            return False
        history = context._history_store
        if history is self.history and history.generation != self.generation:
            return False
        # Disk restore or a cloned manual transaction can own another store.
        # Exact prefix equality also catches in-place edits outside append().
        return len(history.messages) >= len(self.prefix) and history.messages[:len(self.prefix)] == self.prefix


class AsyncCompaction:
    def __init__(self):
        self.current: CompactionCandidate | None = None
        self.last_attempt: tuple | None = None
        self.tasks: dict[asyncio.Task, Any] = {}
        self.closing = False

    def pending_tasks(self, *, llm=None):
        return {task for task, adapter in self.tasks.items() if not task.done() and (llm is None or adapter is llm)}

    async def aclose(self):
        self.closing = True
        self.current = None
        pending = self.pending_tasks()
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def maybe_start(self, context, state, *, execution, tools, metadata, run_context,
                    rollout_budget, phase, messages=None):
        if self.closing or not enabled(context, execution):
            return
        if context._tool_execution_context is not None and context._tool_execution_context.cancel_event is not None:
            if context._tool_execution_context.cancel_event.is_set():
                return
        adapter = execution.llm
        if phase == "pre_turn" and not adapter.has_compaction_route(metadata):
            return
        limit = adapter.capabilities.context_window
        if not limit or not context._history or context._compaction_inflight_start() != len(context._history):
            return
        budget = context.get_budget_snapshot(state, tool_schemas=tools, messages=messages)
        if budget["used"] >= context._budget.total - context._budget.reserved_response_tokens:
            return  # Foreground compaction already owns the exhausted window.
        if budget["used"] * 100 < limit * threshold_percent(execution.config):
            return
        identity = adapter.compaction_identity()
        history = context._history_store
        attempt = (id(history), history.generation, identity)
        if self.last_attempt == attempt:
            return
        if self.current is not None and self.current.matches(context, execution):
            return
        if messages is None:
            messages = context._render_prompt_messages(state, context._workspace_root_for_state(state))
        captured = deepcopy({"prefix": context._history, "messages": messages, "tools": tools})
        from backend.llm.compaction_v2 import prepare_request

        request = prepare_request(adapter, captured["messages"], tools=captured["tools"], metadata=metadata)
        candidate = CompactionCandidate(history, history.generation, captured["prefix"], identity, adapter)
        self.last_attempt = attempt
        self.current = candidate
        operation = "background-compaction-" + uuid4().hex
        turn = LLMTurnContext(cost_session_id=(run_context.llm_turn_context.cost_session_id
            if run_context.llm_turn_context is not None else run_context.cost_session_id),
            lifecycle_runtime=run_context.lifecycle_runtime)
        goal = run_context.goal_accounting
        if goal is not None:
            goal.register_usage(operation, turn.usage)
        request_metadata = dict(metadata)

        async def generate():
            try:
                candidate.replacement = await adapter.compact_context_v2(captured["messages"],
                    tools=captured["tools"], metadata=request_metadata, turn_context=turn, background=True, prepared=request)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                candidate.error = str(error)
                logger.debug("Background compaction failed", exc_info=True)
            finally:
                # These are independent request totals, including discarded candidates.
                rollout_budget.record_usage_total(operation, turn.usage)
                if goal is not None:
                    goal.account()
                journal = run_context.execution_journal
                if journal is not None:
                    await to_thread_cancel_safe(journal.append_lifecycle, "background_compaction_finished", {
                        "operation_id": operation, "records": deepcopy(turn.side_call_records),
                        "usage": dict(vars(turn.usage)), "prepared": candidate.replacement is not None,
                    })

        task = asyncio.create_task(generate(), name=operation)
        candidate.task = task
        self.tasks[task] = adapter
        if run_context.retain_model is not None:
            run_context.retain_model(adapter, task)
        if run_context.retain_background_model is not None:
            run_context.retain_background_model(task)

        def finished(done):
            self.tasks.pop(done, None)
            if not done.cancelled() and (error := done.exception()) is not None:
                candidate.error = str(error)
                logger.error("Background compaction accounting failed", exc_info=(type(error), error, error.__traceback__))

        task.add_done_callback(finished)

    def ready(self, context, execution) -> bool:
        candidate = self.current
        if candidate is None:
            return False
        if not enabled(context, execution) or not candidate.matches(context, execution):
            self.current = None
            return False
        if not candidate.task.done():
            return False
        if candidate.task.cancelled() or candidate.replacement is None or candidate.error:
            self.current = None
            return False
        return True

    async def take(self, context, execution, *, wait: bool):
        candidate = self.current
        if candidate is None or not enabled(context, execution):
            return None
        if not candidate.matches(context, execution):
            self.current = None
            return None
        if not candidate.task.done():
            if not wait:
                return None
            await asyncio.shield(candidate.task)
        if self.current is candidate:
            self.current = None
        if candidate.task.cancelled() or candidate.replacement is None or candidate.error:
            return None
        candidate.task.result()
        return candidate if candidate.matches(context, execution) else None
