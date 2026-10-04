from __future__ import annotations

from copy import deepcopy

import pytest

from backend.llm.anthropic_adapter import AnthropicAdapter
from backend.llm.base import LLMMessage, ToolCallEvent


def _images(prefix: str, count: int):
    return [{"media_type": "image/png", "data": f"{prefix}_{index}"} for index in range(count)]


def _media_and_notices(messages):
    media, notices = [], []
    for message in messages:
        if not isinstance(message["content"], list):
            continue
        for block in message["content"]:
            blocks = block["content"] if block["type"] == "tool_result" and isinstance(block["content"], list) else [block]
            for item in blocks:
                if item["type"] in {"image", "document"}:
                    media.append(item["source"]["data"])
                elif item["type"] == "text" and "Media omitted" in item["text"]:
                    notices.append(item["text"])
    return media, notices


@pytest.mark.parametrize("tagged", [True, False])
def test_tool_images_do_not_evict_current_user_attachment(tagged):
    messages = [
        LLMMessage(role="user", content="Inspect my picture", is_user_input=tagged, images=_images("CURRENT", 1)),
        LLMMessage(role="assistant", content="", tool_calls=[ToolCallEvent(id="read", name="read_file", arguments={})]),
        LLMMessage(role="tool", content="Tool screenshot text", tool_call_id="read", images=_images("TOOL", 100)),
    ]
    original = deepcopy(messages)
    _, request = AnthropicAdapter._convert_messages(messages)
    media, notices = _media_and_notices(request)
    assert "CURRENT_0" in media
    assert len(media) == 100
    assert len(notices) == 1
    assert messages == original
    tool_block = request[-1]["content"][0]
    assert tool_block["tool_use_id"] == "read"
    assert tool_block["content"][-1]["text"] == "Tool screenshot text"


def test_tagged_current_input_survives_merged_following_runtime_user_context():
    messages = [
        LLMMessage(role="user", content="Old input", images=_images("OLD", 100)),
        LLMMessage(role="assistant", content="Old answer"),
        LLMMessage(role="user", content="Current input", is_user_input=True, images=_images("CURRENT", 1)),
        LLMMessage(role="user", content="Runtime reminder"),
    ]
    _, request = AnthropicAdapter._convert_messages(messages)
    media, notices = _media_and_notices(request)
    assert len(media) == 100 and "CURRENT_0" in media
    assert "OLD_0" not in media
    assert len(notices) == 1


def test_current_user_pdf_and_images_fill_limit_without_being_discarded_for_tool_media():
    current = LLMMessage(
        role="user", content="Current input", is_user_input=True, images=_images("CURRENT", 99),
        documents=[{"media_type": "application/pdf", "file_name": "current.pdf", "data": "CURRENT_PDF"}],
    )
    messages = [
        current,
        LLMMessage(role="assistant", content="", tool_calls=[ToolCallEvent(id="read", name="read_file", arguments={})]),
        LLMMessage(role="tool", content="Tool images", tool_call_id="read", images=_images("TOOL", 10)),
    ]
    _, request = AnthropicAdapter._convert_messages(messages)
    media, notices = _media_and_notices(request)
    assert len(media) == 100 and "CURRENT_PDF" in media
    assert not any(data.startswith("TOOL") for data in media)
    assert len(notices) == 10
    assert current.documents[0]["data"] == "CURRENT_PDF"


def test_sdk_current_input_above_limit_is_explicitly_rejected_without_mutation():
    message = LLMMessage(role="user", content="Current input", images=_images("CURRENT", 101))
    with pytest.raises(ValueError, match="current user message exceeds"):
        AnthropicAdapter._convert_messages([message])
    assert len(message.images) == 101
