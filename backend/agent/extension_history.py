"""Private-state undo facts at retained model-history boundaries."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from backend.conversations.projection_log import apply_value_change

REWIND_KEY = "_history_rewind"
DELIVERY_KEYS = ("pending_messages", "followups")


def extension_values(state: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in state.items() if key not in (REWIND_KEY, *DELIVERY_KEYS)}


def rewind_extension_state(
    state: dict[str, Any], *, history_end: int, current_history_end: int,
) -> dict[str, Any]:
    """Restore retained private state; delivery queues belong to their source run."""
    result = deepcopy(state)
    timeline = result.pop(REWIND_KEY, None)
    floor = timeline["floor"] if timeline is not None else current_history_end if extension_values(state) else 0
    if history_end < floor:
        raise ValueError("Extension state for this history boundary was not recorded; choose a later message.")
    if timeline is not None:
        changes = timeline["changes"]
        while changes and changes[-1]["history_end"] > history_end:
            result = apply_value_change(result, changes.pop()["undo"], in_place=True)
        result[REWIND_KEY] = timeline
    for key in DELIVERY_KEYS:
        result.pop(key, None)
    return result


def rebase_extension_history(state: dict[str, Any], *, removed_prefix: int, inserted_prefix: int) -> None:
    """Compaction absorbs old undo facts and relocates the retained tail."""
    if REWIND_KEY not in state:
        return
    timeline = state[REWIND_KEY]
    timeline["floor"] = max(inserted_prefix, timeline["floor"] - removed_prefix + inserted_prefix)
    timeline["changes"] = [
        {**item, "history_end": item["history_end"] - removed_prefix + inserted_prefix}
        for item in timeline["changes"] if item["history_end"] > removed_prefix
    ]
