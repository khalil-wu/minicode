from __future__ import annotations

from backend.conversations import public_projection
from backend.conversations.repository import _normalize_loaded_transcript


def test_arbitrary_public_json_uses_one_message_wide_text_budget(monkeypatch) -> None:
    monkeypatch.setattr(public_projection, "_MAX_PUBLIC_JSON_TEXT_CHARS", 32)

    projected = public_projection.project_public_transcript_message(
        {
            "id": "message-1",
            "role": "custom",
            "content": [{"type": "custom", "value": "a" * 24}],
            "artifacts": [{"label": "b" * 24}],
        }
    )

    assert projected["content"][0]["value"] == "a" * 24
    assert projected["artifacts"][0]["label"] == "bb"


def test_transcript_projection_drops_obsolete_added_tool_names() -> None:
    projected = public_projection.project_public_transcript_message(
        {
            "id": "message-1",
            "role": "assistant",
            "content": "done",
            "added_tool_names": ["read_file"],
            "addedToolNames": ["grep_files"],
        }
    )

    assert "added_tool_names" not in projected
    assert "addedToolNames" not in projected


def test_transcript_projection_keeps_summary_and_drops_raw_provider_reasoning() -> None:
    projected = public_projection.project_public_transcript_message({
        "id": "assistant-1",
        "role": "assistant",
        "content": "done",
        "blocks": [
            {
                "type": "thinking",
                "content": "raw body",
                "source": "provider",
                "provider_reasoning_type": "reasoning_content",
            },
            {
                "type": "thinking",
                "content": "durable summary",
                "source": "provider",
                "provider_reasoning_type": "reasoning_summary_text",
            },
        ],
    })

    assert projected["blocks"] == [{
        "type": "thinking",
        "content": "durable summary",
        "source": "provider",
        "provider_reasoning_type": "reasoning_summary_text",
    }]


def test_loaded_transcript_treats_untyped_provider_thinking_as_legacy_raw() -> None:
    normalized = _normalize_loaded_transcript([{
        "id": "assistant-legacy",
        "role": "assistant",
        "content": "done",
        "blocks": [
            {"type": "thinking", "content": "legacy raw", "source": "provider"},
            {
                "type": "thinking",
                "content": "summary",
                "source": "provider",
                "providerReasoningType": "reasoning_summary_text",
            },
        ],
    }])

    assert normalized[0]["blocks"] == [{
        "type": "thinking",
        "content": "summary",
        "source": "provider",
        "provider_reasoning_type": "reasoning_summary_text",
    }]
