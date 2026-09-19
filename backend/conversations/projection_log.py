"""Incremental materialized conversation projections behind a manifest commit."""
from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any


def value_change(before: Any, after: Any) -> dict[str, Any] | None:
    """Encode only changed JSON fields, list positions, and appended text."""
    if before is after or (type(before) is type(after) and not isinstance(before, (dict, list)) and before == after):
        return None
    if isinstance(before, str) and isinstance(after, str) and after.startswith(before):
        return {"append": after[len(before):], "at": len(before)}
    if isinstance(before, dict) and isinstance(after, dict):
        changed = {}
        for key, value in after.items():
            change = value_change(before[key], value) if key in before else {"value": value}
            if change is not None:
                changed[key] = change
        removed = [key for key in before if key not in after]
        return {"fields": changed, "remove": removed} if changed or removed else None
    if isinstance(before, list) and isinstance(after, list):
        changed = {}
        for index, value in enumerate(after):
            change = value_change(before[index], value) if index < len(before) else {"value": value}
            if change is not None:
                changed[str(index)] = change
        return {"items": changed, "length": len(after)} if changed or len(before) != len(after) else None
    return {"value": after}


def apply_value_change(before: Any, change: dict[str, Any] | None, *, in_place: bool = False) -> Any:
    """Decode a persisted change, rejecting offsets that address another base."""
    if change is None:
        return before
    if "value" in change:
        return deepcopy(change["value"]) if in_place else change["value"]
    if "append" in change:
        if not isinstance(before, str) or len(before) != change["at"]:
            raise ValueError("Projection text offset does not address its base")
        return before + change["append"]
    if "fields" in change:
        old_values = {key: before.get(key) for key in change["fields"]}
        result = before if in_place else dict(before)
        for key in change["remove"]:
            result.pop(key, None)
        for key, patch in change["fields"].items():
            result[key] = apply_value_change(old_values[key], patch, in_place=in_place)
        return result
    if "items" in change:
        length = change["length"]
        if not isinstance(length, int) or isinstance(length, bool) or length < 0:
            raise ValueError("Invalid projection list length")
        original_length = len(before)
        if any(str(index) not in change["items"] for index in range(original_length, length)):
            raise ValueError("Projection list change is missing its base or appended values")
        if in_place:
            result = before
            del result[length:]
            result.extend([None] * max(0, length - len(result)))
        else:
            result = before[:length] + [None] * max(0, length - len(before))
        for key, patch in change["items"].items():
            index = int(key)
            if not 0 <= index < length:
                raise ValueError("Projection list index does not address its base")
            result[index] = apply_value_change(result[index] if index < original_length else None, patch, in_place=in_place)
        return result
    raise ValueError("Unknown projection change")


def append_projection(
    path: Path, *, committed_bytes: int, revision: int,
    previous: dict[str, Any], current: dict[str, Any], seed_revision: int | None = None,
) -> int:
    """Write and sync before publishing the returned byte position in the manifest.

    The conversation writer already owns the file lock. Bytes beyond the prior
    manifest position belong to a failed publication and are replaced on retry.
    """
    entries = []
    if seed_revision is not None:
        entries.append({"revision": seed_revision, "change": {"value": previous}})
    entries.append({"revision": revision, "change": value_change(previous, current)})
    encoded = "".join(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n" for entry in entries).encode("utf-8")
    with path.open("r+b" if path.exists() else "w+b") as handle:
        handle.seek(committed_bytes)
        handle.truncate()
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
        return handle.tell()


def read_projection(path: Path, *, committed_bytes: int, revision: int) -> dict[str, Any]:
    """Read exactly the committed prefix; an incomplete committed entry is corruption."""
    with path.open("rb") as handle:
        raw = handle.read(committed_bytes)
    if len(raw) != committed_bytes or not raw.endswith(b"\n"):
        raise ValueError("Committed conversation projection is truncated")
    result: dict[str, Any] = {}
    previous_revision = 0
    for line in raw.splitlines():
        entry = json.loads(line)
        current_revision = entry["revision"]
        if (isinstance(current_revision, bool) or not isinstance(current_revision, int)
                or not previous_revision < current_revision <= revision):
            raise ValueError("Conversation projection revisions are out of order")
        result = apply_value_change(result, entry["change"])
        previous_revision = current_revision
    if previous_revision != revision or not isinstance(result, dict):
        raise ValueError("Conversation projection does not match its manifest")
    return result
