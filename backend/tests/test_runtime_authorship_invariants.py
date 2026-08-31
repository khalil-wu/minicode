"""Internal provider-alternation placeholders never persist across turns."""

from __future__ import annotations

from backend.agent.context import INTERNAL_EMPTY_ASSISTANT_MARKER, ContextBuilder
from backend.config import TokenBudget


def _builder() -> ContextBuilder:
    return ContextBuilder(TokenBudget())


def test_the_empty_placeholder_stays_out_of_exported_snapshots() -> None:
    # ``export_snapshot`` is deliberately lossless: it is the authoritative
    # resume/replay boundary and must reproduce the exact provider-visible
    # message bodies (rewriting them there would change the next stateless
    # request and defeat prefix caching). The placeholder is dropped on the
    # *restore* boundary instead -- ``sanitize_snapshot_history``, which
    # ``load_snapshot`` runs -- so a legacy snapshot carrying "(empty)" can
    # never resurrect it into a live session. No current code path even
    # writes the marker into history; the filter exists purely for snapshots
    # written by older builds.
    builder = _builder()
    builder.append_user("hello")
    builder.append_assistant(INTERNAL_EMPTY_ASSISTANT_MARKER)

    legacy_snapshot = builder.export_snapshot()

    restored = _builder()
    restored.load_snapshot(legacy_snapshot)

    assert [(message.role, message.content) for message in restored._history] == [
        ("user", "hello")
    ], "the alternation placeholder must not survive a snapshot restore"
    assert all(
        str(entry.get("content", "")).strip() != INTERNAL_EMPTY_ASSISTANT_MARKER
        for entry in restored.export_snapshot().get("history", [])
    ), "the alternation placeholder must not persist across sessions"
    assert all(
        str(entry.get("content", "")).strip() != INTERNAL_EMPTY_ASSISTANT_MARKER
        for entry in ContextBuilder.sanitize_snapshot_history(
            legacy_snapshot.get("history", [])
        )
    )
