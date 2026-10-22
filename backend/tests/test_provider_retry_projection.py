"""A provider retry survives both live stream restore and durable turn storage."""
from backend.agent.progress import provider_stream_error_progress
from backend.agent.provider_attempt import ProviderAttempt
from backend.agent.turn_state import AgentTurnState
from backend.conversations.public_projection import project_public_transcript_message
from backend.ws.stream_state import apply_stream_event, create_stream_state


def test_retry_evidence_is_public_and_is_not_erased_by_liveness_completion():
    turn = AgentTurnState(now_ms=lambda: 1000)
    streams = {"A": create_stream_state("A", "answer-A", turn_id="turn-A")}
    operation = "provider:connection:turn-A:iteration-1"
    notices = []
    for retry in (1, 2):
        event = provider_stream_error_progress(
            f"连接中断，正在重连（第 {retry}/5 次）",
            attempt=ProviderAttempt("iteration-1", retry - 1, f"span-{retry}", 1000),
            progress_id=operation, error_message=f"stream closed {retry}",
            retry_attempt=retry, max_retries=5, retry_after_ms=200,
        )
        notices.append(event.data["id"])
        turn.record_progress(event.data)
        apply_stream_event(streams, "A", event.type, event.data)
    completed = {"id": operation, "stage": "status", "status": "completed", "message": "Response completed",
                 "provider_state": "completed", "visibility": "debug"}
    turn.record_progress(completed)
    apply_stream_event(streams, "A", "agent.progress", completed)
    snapshot = turn.finalize(terminal_status="completed")
    public = project_public_transcript_message({"id": "answer-A", "role": "assistant", "content": "", "blocks": snapshot.blocks})
    assert [block["id"] for block in public["blocks"]] == notices
    assert [block["retryAttempt"] for block in public["blocks"]] == [1, 2]
    assert all(block["status"] == "info" for block in public["blocks"])
    assert [block["id"] for block in streams["A"]["content_blocks"]] == notices
