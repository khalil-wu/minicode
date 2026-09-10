"""Internal provider-alternation placeholders never persist across turns."""

from __future__ import annotations

from backend.agent.context import INTERNAL_EMPTY_ASSISTANT_MARKER, ContextBuilder
from backend.config import TokenBudget


def _builder() -> ContextBuilder:
    return ContextBuilder(TokenBudget())


def test_literal_empty_marker_survives_snapshot_round_trip() -> None:
    # Snapshot import cannot infer runtime authorship from message text.
    builder = _builder()
    builder.append_user("hello")
    builder.append_assistant(INTERNAL_EMPTY_ASSISTANT_MARKER)
    snapshot = builder.export_snapshot()
    restored = _builder()
    restored.load_snapshot(snapshot)
    assert [(message.role, message.content) for message in restored._history] == [
        ("user", "hello"), ("assistant", INTERNAL_EMPTY_ASSISTANT_MARKER),
    ]
    assert restored.export_snapshot()["history"] == snapshot["history"]
