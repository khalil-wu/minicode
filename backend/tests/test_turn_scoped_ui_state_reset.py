from backend.ws.agent_runner import (
    UI_AGENT_STATE_SNAPSHOT_KEY,
    _reset_ui_agent_state_snapshot,
)


def test_new_turn_resets_ui_agent_state_snapshot_without_dropping_history():
    snapshot = {
        "history": [{"role": "user", "content": "old task"}],
        "persistent_notes": [{"kind": "summary", "content": "keep this"}],
        UI_AGENT_STATE_SNAPSHOT_KEY: {
            "plan": {
                "planId": "angry-birds-plan",
                "status": "executing",
                "currentStep": 0,
                "steps": [{"id": "s1", "title": "write angry birds", "status": "running"}],
            },
            "todos": [{"id": "todo-1", "content": "write angry birds", "status": "in_progress"}],
            "subagents": [],
            "agentProgress": [{"id": "plan:angry-birds-plan", "status": "running"}],
        },
    }

    reset = _reset_ui_agent_state_snapshot(snapshot)

    assert reset["history"] == snapshot["history"]
    assert reset["persistent_notes"] == snapshot["persistent_notes"]
    assert reset[UI_AGENT_STATE_SNAPSHOT_KEY] == {
        "plan": None,
        "todos": [],
        "subagents": [],
        "agentProgress": [],
    }
