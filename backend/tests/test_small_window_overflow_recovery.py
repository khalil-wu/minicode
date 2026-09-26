"""A model whose real window is smaller than the configured budget stays usable.

The configured default window is large, so for such models reactive compaction
after the provider's overflow error is the recovery path. It must recognise the
provider's wording and must be available again after the provider accepts a
compacted prompt, within the same user turn.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from backend.agent.error_withholding import is_context_overflow_error
from backend.agent.loop import run_agent_loop
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings, TokenBudget
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent, estimate_llm_context_tokens
from backend.llm.errors import classify_llm_error, sanitize_llm_error_message
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.base import BaseTool, ToolResult, ToolSchema
from backend.tools.registry import ToolRegistry

REAL_WINDOW = 45_000


@pytest.mark.parametrize(
    "message",
    [
        "Range of input length should be [1, 30720]",
        "This model's maximum prompt length is 131072 but the request contains 537812 tokens",
        "Your request exceeded model token limit: 262144 (requested: 300000)",
        "the request exceeds the available context size, try increasing it",
        "tokens to keep from the initial prompt is greater than the context length",
        "The input token count (1196265) exceeds the maximum number of tokens allowed (1048575)",
        "The input (140000 tokens) is longer than the model's context length (131072 tokens).",
        "prompt too long; exceeded max context length by 812 tokens",
    ],
)
def test_provider_overflow_wordings_enter_reactive_compaction(message: str) -> None:
    assert classify_llm_error(message).error_type == "prompt_too_long"
    assert is_context_overflow_error(message)
    assert "/compact" in sanitize_llm_error_message(message)


@pytest.mark.parametrize(
    "message",
    [
        "ThrottlingException: Too many tokens, please wait before trying again.",
        "Rate limit reached for gpt-x in organization org-1 on tokens per min (TPM)",
    ],
)
def test_throttling_that_mentions_tokens_is_not_an_overflow(message: str) -> None:
    assert classify_llm_error(message).error_type != "prompt_too_long"


def _qwen_overflow(tokens: int) -> httpx.HTTPStatusError:
    body = {"error": {"message": f"Range of input length should be [1, {REAL_WINDOW}]", "type": "invalid_request_error"}}
    request = httpx.Request("POST", "https://dashscope.invalid/compatible-mode/v1/chat/completions")
    response = httpx.Response(400, request=request, content=json.dumps(body).encode())
    return httpx.HTTPStatusError(f"Client error '400 Bad Request' ({tokens} tokens)", request=request, response=response)


class _SmallWindowLLM(LLMAdapter):
    def __init__(self, tool_rounds: int) -> None:
        self.tool_rounds = tool_rounds
        self.rounds_done = 0
        self.overflows = 0
        self.compactions = 0

    async def stream_chat(self, messages, tools=None, metadata=None):
        tokens = estimate_llm_context_tokens(messages, tools)
        if tokens > REAL_WINDOW:
            self.overflows += 1
            raise _qwen_overflow(tokens)
        if self.rounds_done < self.tool_rounds:
            self.rounds_done += 1
            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=[ToolCallEvent(
                id=f"call_{self.rounds_done}", name="big_read", arguments={"n": self.rounds_done})])
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")
            return
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="All files reviewed.")
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

    async def simple_chat(self, messages, *, max_tokens=None):
        self.compactions += 1
        return "## Goal\nReview files.\n## Progress\nReviewed the earlier files."


class _BigReadTool(BaseTool):
    name = "big_read"
    description = "Read one large file."
    read_only = True

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={"type": "object", "properties": {"n": {"type": "integer"}}},
        )

    async def execute(self, args, context=None):
        return ToolResult(content=(f"file {args.get('n')} line\n" * 2_000)[:30_000], status="success")


def test_long_turn_recovers_from_every_overflow(tmp_path: Path) -> None:
    llm = _SmallWindowLLM(tool_rounds=12)
    registry = ToolRegistry()
    registry.register(_BigReadTool())

    async def run() -> list:
        events = []
        async for event in run_agent_loop(
            user_message="Review every file in the repo.",
            llm=llm,
            tool_registry=registry,
            artifact_store=ArtifactStore(storage_dir=str(tmp_path / "artifacts")),
            permission_checker=PermissionChecker(settings=PermissionSettings(), workspace_root=tmp_path),
            agent_settings=AgentSettings(max_iterations=60),
            token_budget=TokenBudget(total=1_000_000),
            permission_context=PermissionContext(mode="bypass"),
        ):
            events.append(event)
        return events

    events = asyncio.run(run())
    done = [event for event in events if event.type == "done"][-1]
    assert llm.overflows >= 2
    assert llm.compactions >= 2
    assert done.data.get("status") == "completed", done.data
    assert llm.rounds_done == 12
