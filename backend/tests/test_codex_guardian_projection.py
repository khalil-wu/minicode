from backend.agent.message import AgentEvent
from backend.ws.stream_state import apply_stream_event, create_stream_state, get_stream_content_blocks
from backend.ws.events import SERVER_EVENT_TYPES
from backend.conversations.public_projection import project_public_transcript_message


def assessment(status="in_progress"):
    return {"id": "review", "target_item_id": "exec", "turn_id": "turn", "started_at_ms": 1000,
        "status": status, "action": {"type": "command", "source": "unified_exec", "command": "pwd", "cwd": "/workspace"}}


def test_guardian_live_snapshot_keeps_terminal_assessment_and_original_warning():
    assert {"guardian_assessment", "guardian_warning"} <= SERVER_EVENT_TYPES
    streams = {"conversation": create_stream_state("conversation", "answer", "turn")}
    event = AgentEvent.guardian_assessment(assessment(), conversation_id="conversation", message_id="answer")
    apply_stream_event(streams, "conversation", event.type, event.data)
    final = AgentEvent.guardian_assessment({**assessment("denied"), "completed_at_ms": 2000,
        "risk_level": "high", "rationale": "Original restriction"}, conversation_id="conversation", message_id="answer")
    apply_stream_event(streams, "conversation", final.type, final.data)
    apply_stream_event(streams, "conversation", event.type, event.data)
    warning = AgentEvent.guardian_warning("Repeated denials", conversation_id="conversation", message_id="answer", turn_id="turn", interruption=True)
    apply_stream_event(streams, "conversation", warning.type, warning.data)
    apply_stream_event(streams, "conversation", warning.type, warning.data)
    blocks = get_stream_content_blocks(streams["conversation"])
    assert len(blocks) == 2
    assert blocks[0] == {"type": "guardian_assessment", "review": {**assessment("denied"), "completed_at_ms": 2000,
        "risk_level": "high", "rationale": "Original restriction"}}
    assert blocks[1] == {"type": "guardian_warning", "message": "Repeated denials", "turnId": "turn", "interruption": True}
    cold = project_public_transcript_message({"id": "answer", "role": "assistant", "content": "", "blocks": blocks})
    assert cold["blocks"] == blocks


def test_guardian_snapshot_does_not_write_to_newer_turn_or_reopen_terminal_stream():
    streams = {"conversation": create_stream_state("conversation", "answer", "turn")}
    apply_stream_event(streams, "conversation", "guardian_assessment", {**assessment(), "message_id": "old-answer"})
    apply_stream_event(streams, "conversation", "guardian_assessment", {**assessment(), "message_id": "answer", "turn_id": "old-turn"})
    assert not get_stream_content_blocks(streams["conversation"])
    apply_stream_event(streams, "conversation", "done", {"message_id": "answer", "turn_id": "turn", "status": "completed"})
    apply_stream_event(streams, "conversation", "guardian_assessment", {**assessment(), "message_id": "answer"})
    assert not get_stream_content_blocks(streams["conversation"])
