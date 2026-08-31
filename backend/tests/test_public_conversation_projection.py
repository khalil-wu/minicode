from __future__ import annotations

from backend.conversations import public_projection


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
