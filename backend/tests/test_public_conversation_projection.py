from __future__ import annotations

from backend.conversations import public_projection
from backend.conversations.repository import _normalize_loaded_transcript


def test_message_projection_preserves_owned_turn_diff_without_exposing_arbitrary_metadata():
    payload = {"thread_id": "conversation", "conversation_id": "conversation", "turn_id": "turn-1", "message_id": "assistant-1",
        "task_id": "task-1", "revision": 6, "diff": "diff --git a/parser.py b/parser.py\n+++ b/parser.py\n@@ -0,0 +1 @@\n+def parse(): pass\n",
        "source": "workspace_snapshot", "workspace_root": "C:/project", "private_transport": "must be dropped"}
    projected = public_projection.project_public_transcript_message({"id": "assistant-1", "role": "assistant", "content": "Done",
        "metadata": {"turn_diff": payload, "private_state": "must be dropped"}})
    diff = projected["metadata"]["turn_diff"]
    assert diff["diff"] == payload["diff"]
    assert diff["message_id"] == "assistant-1" and diff["revision"] == 6
    assert "private_transport" not in diff and "private_state" not in projected["metadata"]


def test_subagent_membership_is_not_limited_by_the_collapsed_preview() -> None:
    rows = [{"id": f"child-{index}", "status": "done", "agentPath": f"/root/{index}"} for index in range(35)]
    projected = public_projection.project_public_conversation({"id": "conversation", "context_snapshot": {
        "ui_agent_state": {"subagents": rows},
    }})
    assert projected["context_snapshot"]["ui_agent_state"]["subagents"] == rows


def test_subagent_inline_result_budget_does_not_drop_task_identity(monkeypatch) -> None:
    monkeypatch.setattr(public_projection, "_MAX_PUBLIC_JSON_TEXT_CHARS", 128)
    rows = [{"id": f"child-{index}", "status": "done", "resultContent": "x" * 80} for index in range(3)]
    projected = public_projection.project_public_conversation({"id": "conversation", "context_snapshot": {
        "ui_agent_state": {"subagents": rows},
    }})["context_snapshot"]["ui_agent_state"]["subagents"]
    assert [row["id"] for row in projected] == ["child-0", "child-1", "child-2"]
    assert projected[0]["resultContent"] == "x" * 80
    assert "resultContent" not in projected[1]
    assert projected[1]["resultAvailable"] is True
    assert projected[2]["resultAvailable"] is True
    assert rows[1]["resultContent"] == "x" * 80


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
