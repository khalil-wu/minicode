from __future__ import annotations

from backend.ws.agent_runner import SessionAgentRunnerMixin
from backend.ws.ui_agent_state_store import UiAgentStateStore


def test_high_frequency_tool_output_does_not_read_conversation_for_ui_state_projection():
    class Repository:
        def get_conversation(self, _conversation_id):
            raise AssertionError("tool output must not load the conversation UI snapshot")

    runner = object.__new__(SessionAgentRunnerMixin)
    runner.__dict__["_ui_agent_state_store"] = UiAgentStateStore()
    runner.conversation_repo = Repository()
    runner._persist_ui_agent_state_event(
        "conversation",
        "tool_output_delta",
        {"tool_call_id": "call", "output": "chunk"},
    )
    runner._persist_ui_agent_state_event(
        "conversation",
        "command_output_chunk",
        {"tool_call_id": "call", "content": "chunk"},
    )
