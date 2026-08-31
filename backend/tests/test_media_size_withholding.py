import asyncio

from backend.agent.context import ContextBuilder
from backend.agent.error_withholding import (
    ErrorWithholdingController,
    is_media_size_error,
)
from backend.agent.state import AgentState
from backend.agent.turn_recovery_runtime import (
    recover_withheld_error as _try_error_withholding_recovery,
)
from backend.config import TokenBudget
from backend.llm.base import LLMMessage, ToolCallEvent
from backend.llm.errors import classify_llm_error, sanitize_llm_error_message


def test_classify_media_size_and_prompt_too_long() -> None:
    media = classify_llm_error(
        "Error code: 400 - image exceeds 5 MB maximum: 5316852 bytes > 5242880 bytes"
    )
    assert media.error_type == "media_size"
    assert media.retryable is True

    ptl = classify_llm_error("HTTP 413 prompt is too long for the model context window")
    assert ptl.error_type == "prompt_too_long"
    assert ptl.retryable is True


def test_classify_unsupported_image_input_as_fatal_provider_capability() -> None:
    classification = classify_llm_error(
        'HTTP 404: {"error":{"message":"No endpoints found that support image input"}}'
    )

    assert classification.error_type == "provider_capability"
    assert classification.provider_error_type == "unsupported_capability"
    assert classification.fatal is True
    assert classification.retryable is False


def test_classify_content_exists_risk_as_content_filter() -> None:
    raw = 'HTTP 400: {"error":{"message":"Content Exists Risk","type":"invalid_request_error"}}'

    classification = classify_llm_error(raw)
    message = sanitize_llm_error_message(raw, classification)

    assert classification.error_type == "blocked"
    assert classification.provider_error_type == "content_filter"
    assert classification.fatal is True
    assert classification.retryable is False
    assert "内容安全策略" in message
    assert "Content Exists Risk" not in message
    assert "invalid_request_error" not in message


def test_is_media_size_error_detects_common_phrases() -> None:
    assert is_media_size_error("image exceeds 5 MB maximum")
    assert is_media_size_error("image dimensions exceed for many-image requests")
    assert not is_media_size_error("rate limit exceeded")


def test_strip_historical_media_keeps_recent_user_attachments() -> None:
    builder = ContextBuilder(token_budget=TokenBudget(total=8_000))
    builder._history_store.append(
        LLMMessage(
            role="user",
            content="old question",
            images=[{"media_type": "image/png", "data": "AAA"}],
            documents=[{"media_type": "application/pdf", "data": "BBB", "file_name": "a.pdf"}],
        )
    )
    builder._history_store.append(LLMMessage(role="assistant", content="old answer"))
    builder._history_store.append(
        LLMMessage(
            role="user",
            content="new question",
            images=[{"media_type": "image/png", "data": "CCC"}],
        )
    )

    stats = builder.strip_historical_media(keep_recent_user_turns=1)
    assert stats["messages"] == 1
    assert stats["images"] == 1
    assert stats["documents"] == 1
    assert builder._history[0].images == []
    assert builder._history[0].documents == []
    assert "media-size recovery" in str(builder._history[0].content)
    assert builder._history[2].images and builder._history[2].images[0]["data"] == "CCC"


def test_media_size_withholding_strips_before_compact() -> None:
    builder = ContextBuilder(token_budget=TokenBudget(total=8_000))
    builder._history_store.append(
        LLMMessage(
            role="user",
            content="old",
            images=[{"media_type": "image/png", "data": "OLDIMG"}],
        )
    )
    builder._history_store.append(
        LLMMessage(
            role="user",
            content="latest",
            images=[{"media_type": "image/png", "data": "NEWIMG"}],
        )
    )
    state = AgentState(user_message="latest")
    controller = ErrorWithholdingController()
    classification = classify_llm_error(
        "image exceeds 5 MB maximum: 9000000 bytes > 5242880 bytes"
    )

    compact_calls = {"n": 0}

    async def _no_compact(s, c):
        compact_calls["n"] += 1
        return False

    recovered = asyncio.run(
        _try_error_withholding_recovery(
            error_controller=controller,
            classification=classification,
            error_content="image exceeds 5 MB maximum",
            state=state,
            ctx=builder,
            compact=_no_compact,
        )
    )
    assert recovered is True
    assert builder._history[0].images == []
    assert builder._history[1].images and builder._history[1].images[0]["data"] == "NEWIMG"
    assert compact_calls["n"] == 0


def test_content_filter_quarantines_only_latest_web_batch_and_recovers() -> None:
    builder = ContextBuilder(token_budget=TokenBudget(total=8_000))
    builder._history_store.append(LLMMessage(role="user", content="今天有什么新闻？"))
    builder._history_store.append(
        LLMMessage(
            role="assistant",
            tool_calls=[
                ToolCallEvent(id="search-1", name="web_search", arguments={"query": "今日新闻"}),
                ToolCallEvent(id="read-1", name="read_file", arguments={"file_path": "README.md"}),
            ],
        )
    )
    builder._history_store.append(
        LLMMessage(role="tool", name="web_search", tool_call_id="search-1", content="rejected source snippets")
    )
    builder._history_store.append(
        LLMMessage(role="tool", name="read_file", tool_call_id="read-1", content="workspace content")
    )
    state = AgentState(user_message="今天有什么新闻？")
    classification = classify_llm_error("HTTP 400 Content Exists Risk")
    compact_calls = {"n": 0}

    async def _no_compact(s, c):
        compact_calls["n"] += 1
        return False

    recovered = asyncio.run(
        _try_error_withholding_recovery(
            error_controller=ErrorWithholdingController(),
            classification=classification,
            error_content="模型服务商因内容安全策略拒绝了本次请求。（provider=content_filter, HTTP 400）",
            state=state,
            ctx=builder,
            compact=_no_compact,
        )
    )

    assert recovered is True
    assert "withheld after provider content-safety rejection" in builder._history[2].content
    assert "rejected source snippets" not in builder._history[2].content
    assert builder._history[3].content == "workspace content"
    assert compact_calls["n"] == 0
    assert builder.quarantine_latest_external_web_results() == 0


def test_content_filter_without_recent_web_results_is_not_retried() -> None:
    builder = ContextBuilder(token_budget=TokenBudget(total=8_000))
    builder._history_store.append(LLMMessage(role="user", content="ordinary request"))
    state = AgentState(user_message="ordinary request")
    classification = classify_llm_error("HTTP 400 Content Exists Risk")

    async def _no_compact(s, c):
        raise AssertionError("content filters must not trigger compaction")

    recovered = asyncio.run(
        _try_error_withholding_recovery(
            error_controller=ErrorWithholdingController(),
            classification=classification,
            error_content="provider_error_type=content_filter status=400",
            state=state,
            ctx=builder,
            compact=_no_compact,
        )
    )

    assert recovered is False


def test_reactive_compaction_runs_only_once_per_turn() -> None:
    builder = ContextBuilder(token_budget=TokenBudget(total=8_000))
    state = AgentState(user_message="latest")
    classification = classify_llm_error("prompt too long")
    compact_calls = {"n": 0}

    async def _compact_once(s, c):
        compact_calls["n"] += 1
        return True

    first = asyncio.run(_try_error_withholding_recovery(
        error_controller=ErrorWithholdingController(),
        classification=classification,
        error_content="prompt too long",
        state=state,
        ctx=builder,
        compact=_compact_once,
    ))
    second = asyncio.run(_try_error_withholding_recovery(
        error_controller=ErrorWithholdingController(),
        classification=classification,
        error_content="prompt too long",
        state=state,
        ctx=builder,
        compact=_compact_once,
    ))

    assert first is True
    assert second is False
    assert compact_calls["n"] == 1


def test_strip_historical_media_single_turn_still_strips() -> None:
    builder = ContextBuilder(token_budget=TokenBudget(total=8_000))
    builder._history_store.append(
        LLMMessage(
            role="user",
            content="only turn",
            images=[{"media_type": "image/png", "data": "ONLY"}],
        )
    )
    stats = builder.strip_historical_media(keep_recent_user_turns=1)
    assert stats["images"] == 1
    assert builder._history[0].images == []

