from backend.ws.event_log import WebSocketReplayEventStore


def test_replay_log_keeps_summary_and_omits_transient_raw_reasoning(tmp_path) -> None:
    store = WebSocketReplayEventStore(session_id="reasoning", root_dir=tmp_path)
    store.append({
        "type": "thinking_delta",
        "content": "raw body",
        "source": "provider",
        "provider_reasoning_type": "reasoning_content",
    })
    store.append({
        "type": "thinking_delta",
        "content": "durable summary",
        "source": "provider",
        "provider_reasoning_type": "reasoning_summary_text",
    })

    assert store.load(limit=10) == [{
        "type": "thinking_delta",
        "content": "durable summary",
        "source": "provider",
        "provider_reasoning_type": "reasoning_summary_text",
    }]
