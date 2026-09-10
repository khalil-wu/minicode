from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.agent.loop import AgentLoopSessionContext
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings, TokenBudget
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.registry import ToolRegistry


@pytest.mark.parametrize("filtered", [False, True])
def test_consumer_close_drains_the_real_query_pipeline(tmp_path: Path, filtered: bool) -> None:
    class Provider(LLMAdapter):
        closed = False

        async def stream_chat(self, messages, tools=None):
            try:
                yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="visible provider output", phase="final_answer")
                await asyncio.Event().wait()
            finally:
                self.closed = True

        async def simple_chat(self, messages):
            return ""

    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", swarm_store_dir=tmp_path / "swarm")
    provider = Provider()
    session = AgentSession(
        llm=provider, tool_registry=ToolRegistry(), artifact_store=ArtifactStore(storage_dir=str(tmp_path / "artifacts")),
        permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
        agent_settings=AgentSettings(), token_budget=TokenBudget(),
    )
    submission = QuerySubmission(
        user_message="show output", session=session,
        runtime=AgentLoopSessionContext(
            workspace_root=tmp_path, permission_context=PermissionContext(mode="bypass"),
            run_context=RunContext(agent_runtime=runtime),
        ),
    )

    async def run() -> None:
        engine = QueryEngine()
        stream = engine.submit_filtered(submission) if filtered else engine.submit(submission)
        try:
            async for event in stream:
                if event.type == "agent_message.delta":
                    break
            else:
                raise AssertionError("provider output never reached the query consumer")
        finally:
            await stream.aclose()
        assert provider.closed, "closing a query must synchronously close its provider"
        assert not session.active_turn
        assert not [task for task in asyncio.all_tasks() if task.get_name().startswith("mailbox-claim-heartbeat-")]

    try:
        asyncio.run(run())
    finally:
        runtime.close()
