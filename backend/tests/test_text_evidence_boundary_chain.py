from __future__ import annotations

import json

import pytest

from backend.agent.prompt_cache import build_prompt_cache_safe_params, prompt_cache_fork_diagnostic
from backend.agent.provider_text_projection import project_provider_text_chunk
from backend.agent.stream_attempt import StreamAttemptState, StreamTextState
from backend.agent.stream_sanitizer import ThinkingStreamSanitizer, scrub_thinking_tags
from backend.agent.value_utils import finite_number, nonnegative_int
from backend.llm.base import LLMMessage, StreamEvent, StreamEventType
from backend.memory.citations import parse_memory_citation


@pytest.mark.asyncio
@pytest.mark.parametrize("split", ["all", "characters", "tag_boundary"])
@pytest.mark.parametrize("body", ["literal example", "<citation_entries>MEMORY.md:1-2|note=[missing close]", "<citation_entries>MEMORY.md:2-1|note=[invalid]</citation_entries>"])
async def test_literal_or_invalid_citation_survives_actual_provider_projection_and_final_scrub(split, body):
    content = f"Before <minicode-memory-citation>{body}</minicode-memory-citation> After"
    chunks = [content] if split == "all" else list(content) if split == "characters" else [content[:23], content[23:]]
    sanitizer = ThinkingStreamSanitizer()
    text = StreamTextState()
    state = StreamAttemptState()
    for chunk in chunks:
        async for _ in project_provider_text_chunk(
            StreamEvent(type=StreamEventType.TEXT_CHUNK, content=chunk, phase="final_answer"),
            stream_state=state, stream_text=text, visible_text_sanitizer=sanitizer,
            provider_raw_final_text={}, live_text_streaming=True, awaiting_trailing_done=False,
            process_event_factory=lambda *args, **kwargs: None,
        ):
            pass
    assert text.full_text + sanitizer.finish() == content
    assert scrub_thinking_tags(text.full_text) == content
    assert sanitizer.citations == []


def test_unclosed_citation_releases_its_literal_content_at_end_of_stream():
    content = "Answer <minicode-memory-citation>literal unfinished"
    sanitizer = ThinkingStreamSanitizer()
    assert sanitizer.feed(content) + sanitizer.finish() == content


@pytest.mark.parametrize("line", ["MEMORY.md:0-1|note=[invalid]", "MEMORY.md:2-1|note=[invalid]", "MEMORY.md:1--2|note=[invalid]", ":1-2|note=[invalid]"])
def test_citation_location_is_validated_once_when_model_metadata_is_parsed(line):
    assert parse_memory_citation([f"<citation_entries>{line}</citation_entries>"]) is None


def test_actual_structured_citation_is_hidden_and_its_usage_metadata_is_retained():
    body = "<citation_entries>MEMORY.md:1-2|note=[remembered]</citation_entries><rollout_ids>run-a</rollout_ids>"
    content = f"Answer<minicode-memory-citation>{body}</minicode-memory-citation> tail"
    sanitizer = ThinkingStreamSanitizer()
    assert sanitizer.feed(content) + sanitizer.finish() == "Answer tail"
    assert parse_memory_citation(sanitizer.citations) == {
        "entries": [{"path": "MEMORY.md", "line_start": 1, "line_end": 2, "note": "remembered"}], "rollout_ids": ["run-a"],
    }
    assert scrub_thinking_tags(content) == "Answer tail"


def test_mcp_cache_diagnostics_compare_actual_tool_identity_without_exposing_names():
    def summary(names):
        return build_prompt_cache_safe_params(messages=[LLMMessage(role="system", content="owned")], tool_schemas=[{"type": "function", "function": {"name": name}} for name in names])

    parent = summary(["mcp__private_server__one", "mcp__private_server__two"])
    foreign = summary(["mcp__other_server__three"])
    subset = summary(["mcp__private_server__one"])
    assert not prompt_cache_fork_diagnostic(parent, foreign)["schema_shadow"]["child_tool_subset_of_parent"]
    assert prompt_cache_fork_diagnostic(parent, subset)["schema_shadow"]["child_tool_subset_of_parent"]
    assert prompt_cache_fork_diagnostic(parent, foreign)["schema_shadow"]["parent_tool_count"] == 2
    assert "private_server" not in json.dumps(parent)
    assert "other_server" not in json.dumps(foreign)


@pytest.mark.parametrize("value", [2**53 + 1, 10**500, 0])
def test_integer_projections_preserve_valid_integer_values_without_float_rounding(value):
    assert nonnegative_int(value) == value
    assert finite_number(value) == value
    assert nonnegative_int(value, maximum=value) == value


def test_numeric_projection_keeps_invalid_and_negative_outcomes_explicit():
    assert nonnegative_int(True) is None
    assert finite_number(False) is None
    assert nonnegative_int(-1) is None
    assert nonnegative_int(2, maximum=1) is None
    assert nonnegative_int(float("inf")) is None
    assert finite_number(float("nan")) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_size", [1, 17, 10000])
@pytest.mark.parametrize("wrapper", [
    "```xml\n{}\n```\n",
    "~~~xml\n{}\n~~~~\n",
    "   ```xml\n{}\n   ```\n",
    "`{}` ",
    "``a ` {} b`` ",
    "````xml\n```\n{}\n````\n",
])
async def test_code_examples_preserve_control_tags_and_citations_through_provider_and_completion(wrapper, chunk_size):
    body = "<citation_entries>MEMORY.md:1-2|note=[example]</citation_entries>"
    citation = f"<minicode-memory-citation>{body}</minicode-memory-citation>"
    example = wrapper.format(f"<think>literal</think><|im_start|>{citation}")
    content = f"Example:\n{example}Answer<think>hidden</think>{citation} tail"
    expected = f"Example:\n{example}Answer tail"
    sanitizer = ThinkingStreamSanitizer()
    text = StreamTextState()
    state = StreamAttemptState()
    for start in range(0, len(content), chunk_size):
        async for _ in project_provider_text_chunk(
            StreamEvent(type=StreamEventType.TEXT_CHUNK, content=content[start:start + chunk_size], phase="final_answer"),
            stream_state=state, stream_text=text, visible_text_sanitizer=sanitizer,
            provider_raw_final_text={}, live_text_streaming=True, awaiting_trailing_done=False,
            process_event_factory=lambda *args, **kwargs: None,
        ):
            pass
    assert text.full_text + sanitizer.finish() == expected
    text.sanitize(scrub_thinking_tags)
    assert text.full_text == expected
    assert scrub_thinking_tags(content) == expected
    assert sanitizer.citations == [body]


@pytest.mark.parametrize("chunk_size", [1, 10000])
def test_fence_closing_requires_its_line_to_end_before_controls_resume(chunk_size):
    content = "```xml\n```more\n<think>literal</think>\n```\n<think>hidden</think>answer"
    sanitizer = ThinkingStreamSanitizer()
    result = "".join(sanitizer.feed(content[i:i + chunk_size]) for i in range(0, len(content), chunk_size)) + sanitizer.finish()
    assert result == "```xml\n```more\n<think>literal</think>\n```\nanswer"
