"""Context changes between durable checkpoints, preserving ordered history."""

from __future__ import annotations

from typing import Any


def rebase_turn_admissions(admissions: dict[str, dict[str, Any]], *, removed_prefix: int, inserted_prefix: int) -> dict[str, dict[str, Any]]:
    return {
        message_id: {**boundary,
                     "history_start": inserted_prefix + int(boundary["history_start"]) - removed_prefix,
                     "history_end": inserted_prefix + int(boundary["history_end"]) - removed_prefix}
        for message_id, boundary in admissions.items()
        if int(boundary["history_start"]) >= removed_prefix and int(boundary["history_end"]) >= removed_prefix
    }


def context_snapshot_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    delta: dict[str, Any] = {
        "set": {key: value for key, value in after.items() if key != "history" and (key not in before or before[key] != value)},
        "removed": [key for key in before if key not in after],
    }
    if "history" in after:
        old, new = before.get("history", []), after["history"]
        prefix = 0
        while prefix < min(len(old), len(new)) and old[prefix] == new[prefix]:
            prefix += 1
        if prefix != len(old) or prefix != len(new) or "history" not in before:
            delta["history_from"] = prefix
            delta["history"] = new[prefix:]
    return delta


def apply_context_snapshot_delta(snapshot: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in snapshot.items() if key not in delta.get("removed", [])}
    result.update(delta.get("set", {}))
    if "history_from" in delta:
        prefix = delta["history_from"]
        history = snapshot.get("history", [])
        # Stored/replayed deltas must refer to their checkpoint, never silently
        # truncate an unrelated history after corruption or a stale replay.
        if isinstance(prefix, bool) or not isinstance(prefix, int) or not 0 <= prefix <= len(history):
            raise ValueError("Context delta does not address its checkpoint history")
        result["history"] = history[:prefix] + delta["history"]
    return result


def compose_context_snapshot_deltas(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    """Compose checkpoint→current and current→next without loading the checkpoint."""
    removed = set(first.get("removed", [])) | set(second.get("removed", []))
    values = {key: value for key, value in first.get("set", {}).items() if key not in second.get("removed", [])}
    values.update(second.get("set", {}))
    removed.difference_update(values)
    result: dict[str, Any] = {"set": values, "removed": sorted(removed)}
    if "history_from" in second:
        second_start = second["history_from"]
        if "history_from" in first and first["history_from"] < second_start:
            result["history_from"] = first["history_from"]
            result["history"] = first["history"][:second_start - first["history_from"]] + second["history"]
        else:
            result["history_from"] = second_start
            result["history"] = second["history"]
        if "history" in first.get("removed", []):
            result["history_from"] = 0
        result["removed"] = [key for key in result["removed"] if key != "history"]
    elif "history_from" in first and "history" not in second.get("removed", []):
        result["history_from"] = first["history_from"]
        result["history"] = first["history"]
    return result
