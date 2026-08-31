from backend.agent.loop_process_events import model_process_text_event
from backend.llm.base import ToolCallEvent


def _call(name: str) -> ToolCallEvent:
    return ToolCallEvent(id=f"call-{name}", name=name, arguments={})


def test_protocol_like_model_text_is_preserved_with_typed_calls() -> None:
    event = model_process_text_event(
        "mcp__github__search_users",
        [_call("mcp__github__search_users")],
        iteration_id="iter:1",
        source="model_preamble",
    )

    assert event is not None
    assert event.type == "agent.item"
    assert event.data["content"] == "mcp__github__search_users"
    assert event.data["status"] == "completed"


def test_normal_process_prose_is_preserved() -> None:
    event = model_process_text_event(
        "I will use web_search to verify the official source.",
        [_call("web_search")],
        iteration_id="iter:2",
        source="model_preamble",
    )

    assert event is not None
    assert event.data["content"] == "I will use web_search to verify the official source."


def test_protocol_like_model_text_is_preserved_before_tool_frame() -> None:
    event = model_process_text_event(
        "webfetchweb_fetch, web_fetch web_search",
        [],
        iteration_id="iter:3",
        source="model_preamble",
    )

    assert event is not None
    assert event.data["content"] == "webfetchweb_fetch, web_fetch web_search"


def test_punctuation_only_model_text_is_not_published() -> None:
    event = model_process_text_event(
        "...",
        [_call("read_file")],
        iteration_id="iter:4",
        source="model_preamble",
    )

    assert event is None
