"""Resolved Source mailbox recipients survive live and durable publication."""
from dataclasses import asdict
import time

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.execution_journal import ExecutionJournal
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.agent.tool_events import tool_call_start_event
from backend.agent.tool_execution import store_result
from backend.api.models import ToolCallRecord as ApiToolCallRecord
from backend.conversations.public_projection import project_public_tool_call
from backend.llm.base import ToolCallEvent
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.services.subagent_service import build_subagent_transcript_messages, project_subagent_transcript_events
from backend.tools.swarm_tools import SourceSendMessageTool


@pytest.mark.asyncio
@pytest.mark.parametrize("root_id,branch_name,target,recipient", [
    ("root-live", "review", "/root", "parent"),
    ("root-restored", "audit", "/root", "parent"),
    ("root-live", "review", "/root/review", "branch"),
    ("root-live", "review", "/root/sibling", "sibling"),
])
async def test_source_destination_receipt_reaches_event_api_and_child_replay(tmp_path, root_id, branch_name, target, recipient):
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", swarm_store_dir=tmp_path / "swarm", enable_lease_heartbeat=False)
    runtime.start_run(run_id=root_id, conversation_id="owner")
    branch = runtime.start_subagent(subagent_id="branch", parent_run_id=root_id, agent_type="default", agent_path_segment=branch_name)
    child = runtime.start_subagent(subagent_id="worker", parent_run_id=branch.subagent_id, agent_type="default", agent_path_segment="worker")
    runtime.start_subagent(subagent_id="sibling", parent_run_id=root_id, agent_type="default", agent_path_segment="sibling")
    context = ToolExecutionContext(permission=PermissionContext(mode="bypass"), conversation_id="owner",
        metadata={"agent_id": child.subagent_id, "mailbox_epoch": child.mailbox_epoch, "source_agent_message_encrypted": False},
        run_context=RunContext(agent_runtime=runtime, multi_agent_version="v2"))
    call = ToolCallEvent("mailbox-call", "send_message", {"target": target, "message": "Actual destination receipt"})
    builder, state = ContextBuilder(), AgentState(user_message="Review")
    builder.append_user("Review")
    builder.append_assistant_tool_calls([call])
    journal = ExecutionJournal(child.subagent_id, base_dir=tmp_path / "journals")
    start = tool_call_start_event(call, started_epoch=time.time(), iteration_id="iter:1", turn_id="child-turn")
    try:
        actual = await SourceSendMessageTool().execute(call.arguments, context)
        assert not actual.is_error, actual.content
        assert actual.runtime_metadata["recipient"] == recipient
        event = store_result(call, actual, builder, state, iteration_id="iter:1", turn_id="child-turn", tool_ctx=context)
        assert event.data["recipient_id"] == recipient
        assert state.tool_calls[-1].recipient_id == recipient
        api = ApiToolCallRecord.from_internal(state.tool_calls[-1]).model_dump()
        assert api["recipient_id"] == recipient
        assert project_public_tool_call(api)["recipientId"] == recipient
        journal.append_tool_use(start.data)
        journal.append_tool_result(event.data, tool_name=call.name)
        journal.append_terminal(status="completed", summary="Delivered")
        raw = [asdict(row) for row in ExecutionJournal(child.subagent_id, base_dir=tmp_path / "journals").read_events()]
        incremental = project_subagent_transcript_events(raw)
        assert next(row["payload"]["record"] for row in incremental if row["event_type"] == "tool_result")["recipientId"] == recipient
        restored = build_subagent_transcript_messages({"events": raw})
        record = next(block["record"] for message in restored for block in message["blocks"] if block["type"] == "tool_call")
        assert record["recipientId"] == recipient
        assert record["args"]["target"] == target
        messages = runtime.list_swarm_messages(conversation_id="owner")
        assert len(messages) == 1
        assert messages[0].recipient_id == recipient
    finally:
        await runtime.aclose(release_lease=True)
