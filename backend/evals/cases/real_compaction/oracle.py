"""External checks for the historical post-compaction budget failure.

Run this file from the isolated MiniCode task checkout with that checkout first
on sys.path. The oracle itself stays outside the agent's workspace.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


workspace = Path.cwd().resolve()
sys.path.insert(0, str(workspace))

import backend
from backend.agent.context import ContextBuilder
from backend.agent.message import AgentEvent
from backend.agent.state import AgentState
from backend.agent.turn_iteration_admission import IterationAdmissionResult, TurnIterationAdmission
from backend.config import TokenBudget
from backend.llm.base import LLMMessage, UsageInfo
from backend.services.context_budget import manage_context_budget
import backend.agent.turn_iteration_admission as admission_module


if not Path(backend.__file__).resolve().is_relative_to(workspace):
    raise RuntimeError("oracle imported backend outside the task checkout")


class BudgetContext:
    def __init__(self) -> None:
        self.token_usage = 900
        self.compact_calls = 0
        self.consecutive_autocompact_failures = 0
        self.hook_manager = None

    def needs_compaction(self, state=None, *, tool_schemas=None):
        return self.token_usage > 850

    async def compact(self, *args, **kwargs):
        self.compact_calls += 1
        self.token_usage = 870
        return "summary"

    def reset_autocompact_failures(self):
        self.consecutive_autocompact_failures = 0

    def context_ledger(self):
        return {
            "estimated_tokens": self.token_usage,
            "actual_tokens": self.token_usage,
            "compaction_count": self.compact_calls,
            "entries": [{
                "category": "history", "label": "History",
                "estimated_tokens": self.token_usage, "item_count": 1,
                "source_count": 0, "sources": [],
            }],
        }


class PostCompactionOracle(unittest.TestCase):
    def test_successful_compaction_reaches_provider_boundary(self):
        ctx = BudgetContext()
        state = AgentState(user_message="continue")

        async def collect():
            return [event async for event in manage_context_budget(
                ctx, state, TokenBudget(total=1000), tool_schemas=[],
            )]

        events = asyncio.run(collect())
        self.assertEqual([event.type for event in events], ["context_compacted"])
        self.assertIsNone(state.stopped_reason)
        self.assertEqual(ctx.compact_calls, 1)

    def test_provider_usage_can_correct_estimate_down(self):
        builder = ContextBuilder(token_budget=TokenBudget(total=100_000))
        state = AgentState(user_message="continue")
        messages = [LLMMessage(role="user", content="request " * 600)]
        tools = [{"type": "function", "function": {
            "name": "read_file", "description": "file read " * 200,
            "parameters": {"type": "object", "properties": {}},
        }}]
        estimated = builder.get_budget_snapshot(state, tool_schemas=tools, messages=messages)["used"]
        builder.begin_provider_request(messages, tools)
        observed = max(1, min(estimated, builder._request_prompt_estimate) - 500)
        builder.record_actual_usage(UsageInfo(input_tokens=observed))

        corrected = builder.get_budget_snapshot(state, tool_schemas=tools, messages=messages)["used"]
        self.assertLess(corrected, estimated)
        self.assertGreater(corrected, 0)

    def test_iteration_admission_keeps_post_compaction_request(self):
        state = AgentState(user_message="continue")
        context = SimpleNamespace(
            needs_compaction=Mock(return_value=True),
            begin_provider_request=Mock(),
        )
        tool_schema_state = object()
        iteration_runtime = SimpleNamespace(
            prepare=AsyncMock(return_value=SimpleNamespace(
                tool_schema_state=tool_schema_state,
                tool_schemas=[], events=[], terminal=False,
            )),
            llm=object(),
            agent_session=SimpleNamespace(token_budget=TokenBudget(total=1000)),
        )
        budget_runtime = SimpleNamespace(
            local_tokens_used=lambda: 0,
            turn_cost_usd=lambda: 0,
            rollout_boundary=lambda: None,
            apply_boundary=AsyncMock(return_value=(None, [])),
        )
        prepared = SimpleNamespace(
            messages=[LLMMessage(role="user", content="continue")],
            prompt_cache_safe_params={},
        )

        async def noop(**kwargs):
            return None

        async def prepare(**kwargs):
            return prepared

        async def compacted(*args, **kwargs):
            yield AgentEvent(type="context_compacted", data={})

        admission = TurnIterationAdmission(
            context=context, state=state, llm=object(),
            iteration_runtime=iteration_runtime,
            deadline_controller=SimpleNamespace(elapsed=lambda: 0),
            turn_budget_controller=SimpleNamespace(evaluate=Mock(return_value=None)),
            budget_runtime=budget_runtime, turn_start_tool_call_count=0,
            chain=SimpleNamespace(next_iteration=lambda: 1, to_log_context=lambda: "oracle"),
            tool_context=SimpleNamespace(model_execution=None, run_context=None),
            token_budget=TokenBudget(total=1000), metadata={}, external_metadata=None,
            emit_event=AsyncMock(), runtime=SimpleNamespace(update_phase=Mock()),
            run_record=SimpleNamespace(run_id="run-oracle", conversation_id="conv-oracle"),
            llm_request_metadata={}, turn_kernel=object(),
        )

        async def collect():
            return [event async for event in admission._admit(
                previous_tool_schema_state=tool_schema_state,
                initial_turn_pending=True, pending_turn_context=[],
            )]

        with (
            patch.object(admission_module, "inject_subagent_mailbox_updates", noop),
            patch.object(admission_module, "inject_parent_notifications", noop),
            patch.object(admission_module, "prepare_turn_context", prepare),
            patch.object(admission_module, "manage_context_budget", compacted),
        ):
            events = asyncio.run(collect())
        result = next(event for event in events if isinstance(event, IterationAdmissionResult))
        self.assertEqual(result.action, "proceed")
        self.assertIsNone(state.stopped_reason)
        context.begin_provider_request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
