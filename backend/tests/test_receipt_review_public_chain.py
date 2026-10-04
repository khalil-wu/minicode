from types import SimpleNamespace

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.state import AgentState
from backend.agent.tool_execution import store_result
from backend.agent.turn_state import AgentTurnState
from backend.llm.base import ToolCallEvent
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.subprocesses import SubprocessTimeoutError, record_unproven_cleanup
from backend.tools.base import execution_exception_result
from backend.tools.registry import ToolRegistry
from backend.tools.search_tools import GrepFilesTool


@pytest.mark.parametrize("status", ["failed", "timeout", "cancelled"])
def test_actual_public_receipt_and_completion_time_survive_store_and_restore(tmp_path, status):
    exception = SubprocessTimeoutError("recorded native search failure")
    record_unproven_cleanup(exception, reaped=False, proc=SimpleNamespace(pid=4242))
    result = execution_exception_result(exception)
    result.status = status
    registry = ToolRegistry()
    registry.register(GrepFilesTool())
    call = ToolCallEvent(id="actual", name="grep_files", arguments={"pattern": "NEEDLE"})
    state = AgentState(user_message="search")
    builder = ContextBuilder(conversation_id="actual-owner", workspace_root=tmp_path)
    builder.append_assistant_tool_calls([call])
    context = ToolExecutionContext(permission=PermissionContext(mode="bypass"), workspace_root=tmp_path, conversation_id="actual-owner")
    event = store_result(call, result, builder, state, turn_id="actual-run", iteration_id="iter:1", tool_ctx=context, tool_registry=registry)
    committed = state.tool_calls[0].result_payload
    assert event.data["completed_at_ms"] == committed["completed_at_ms"]
    assert event.data["cleanup_receipt"] == committed["cleanup_receipt"]
    ui = AgentTurnState(now_ms=lambda: event.data["completed_at_ms"] + 100_000)
    ui.record_tool_call({"id": "actual", "name": "grep_files", "turn_id": "actual-run", "iteration_id": "iter:1"})
    ui.record_tool_result(event.data)
    restored = ui.finalize(terminal_status=status).tool_calls[0]
    assert restored["finishedAt"] == committed["completed_at_ms"]
    assert restored["cleanupReceipt"]["resource_id"] == "4242"
    assert restored["cleanupReceipt"]["pending"] == 1
    event.data["cleanup_receipt"]["pending"] = 0
    event.data["completed_at_ms"] = 0
    assert committed["cleanup_receipt"]["pending"] == 1 and committed["completed_at_ms"] > 0
