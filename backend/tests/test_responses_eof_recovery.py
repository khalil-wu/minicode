"""Replay-safe recovery for the real Responses EOF observed in the final audit."""
import asyncio
import json

import httpx

from backend.agent.loop import AgentLoopSessionContext
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, LLMSettings, PermissionSettings, TokenBudget
from backend.llm.openai_adapter import OpenAIAdapter
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.registry import ToolRegistry
from backend.tools.write_file import WriteFileTool


def test_responses_eof_retracts_incomplete_call_and_retries_before_any_write(tmp_path):
    requests = []

    def transport(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            events = [{"type": "response.output_item.added", "output_index": 0, "item": {
                "type": "function_call", "id": "item-partial", "call_id": "partial", "name": "write_file", "arguments": ""}},
                {"type": "response.function_call_arguments.delta", "item_id": "item-partial",
                 "output_index": 0, "delta": '{"file_path":"must-not-exist.txt","content":"unfinished'}]
        elif len(requests) == 2:
            call = {"type": "function_call", "id": "item-valid", "call_id": "valid", "name": "write_file",
                    "arguments": json.dumps({"file_path": "result.txt", "content": "Written once"}), "status": "completed"}
            events = [{"type": "response.output_item.added", "output_index": 0, "item": call},
                      {"type": "response.completed", "response": {"id": "r2", "status": "completed", "output": [call],
                       "usage": {"input_tokens": 20, "output_tokens": 10}}}]
        else:
            events = [{"type": "response.completed", "response": {"id": "r3", "status": "completed", "output": [
                {"type": "message", "id": "answer", "role": "assistant", "status": "completed",
                 "content": [{"type": "output_text", "text": "Done", "annotations": []}]}],
                "usage": {"input_tokens": 30, "output_tokens": 2}}}]
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content="".join("data: " + json.dumps(event) + "\n\n" for event in events).encode())

    class WriteOnce(WriteFileTool):
        calls = 0

        async def execute(self, args, context=None):
            self.calls += 1
            return await super().execute(args, context)

    writer = WriteOnce()

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            adapter = OpenAIAdapter(LLMSettings(api_key="test", base_url="https://provider.invalid/v1",
                                               model="gpt-6-luna", wire_api="responses", reasoning_effort="high"), http_client=client)
            registry = ToolRegistry()
            registry.register(writer)
            session = AgentSession(llm=adapter, tool_registry=registry,
                artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
                permission_checker=PermissionChecker(PermissionSettings(), workspace_root=tmp_path),
                agent_settings=AgentSettings(max_iterations=4, stream_max_attempts=2, stream_retry_delay_seconds=0),
                token_budget=TokenBudget())
            try:
                return [event async for event in QueryEngine().submit(QuerySubmission(
                    user_message="Write result.txt", session=session,
                    runtime=AgentLoopSessionContext(workspace_root=tmp_path, permission_context=PermissionContext(mode="bypass")),
                    state=AgentState(user_message="Write result.txt", workspace_root=tmp_path),
                ))]
            finally:
                await session.aclose()

    events = asyncio.run(run())
    assert len(requests) == 3
    assert all(request["reasoning"]["effort"] == "high" for request in requests)
    assert writer.calls == 1, [(event.type, event.data) for event in events if event.type in {"error", "tool_result"}]
    assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "Written once"
    assert not (tmp_path / "must-not-exist.txt").exists()
    assert any(event.type == "agent.progress" and event.data.get("provider_state") == "reconnecting" for event in events)
    assert [event.data["status"] for event in events if event.type == "done"] == ["completed"]
