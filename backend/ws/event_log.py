from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

WS_REPLAY_MAX_STRING_CHARS = 16_000
WS_REPLAY_MAX_LIST_ITEMS = 200
WS_REPLAY_MAX_DICT_ITEMS = 200
WS_REPLAY_DATA_URL_OMITTED = "[data URL omitted from websocket replay]"
WS_REPLAY_LIST_TRUNCATED = "[list truncated for websocket replay]"
WS_REPLAY_DICT_TRUNCATED = "[object truncated for websocket replay]"

_OMIT = object()


def _is_data_url(value: str) -> bool:
    return value[:5].lower() == "data:"


def _truncate_replay_string(value: str, path: str, truncated_fields: list[str]) -> str:
    if len(value) <= WS_REPLAY_MAX_STRING_CHARS:
        return value
    truncated_fields.append(path)
    omitted = len(value) - WS_REPLAY_MAX_STRING_CHARS
    return (
        value[:WS_REPLAY_MAX_STRING_CHARS]
        + f"\n[... websocket replay truncated {omitted} chars ...]"
    )


def _sanitize_replay_value(
    value: Any,
    path: str,
    *,
    omitted_fields: list[str],
    truncated_fields: list[str],
    omit_dict_values: bool,
) -> Any:
    if isinstance(value, str):
        if _is_data_url(value):
            omitted_fields.append(path)
            return _OMIT if omit_dict_values else WS_REPLAY_DATA_URL_OMITTED
        return _truncate_replay_string(value, path, truncated_fields)

    if isinstance(value, list):
        result: list[Any] = []
        for index, item in enumerate(value[:WS_REPLAY_MAX_LIST_ITEMS]):
            item_path = f"{path}[{index}]"
            sanitized = _sanitize_replay_value(
                item,
                item_path,
                omitted_fields=omitted_fields,
                truncated_fields=truncated_fields,
                omit_dict_values=False,
            )
            result.append(WS_REPLAY_DATA_URL_OMITTED if sanitized is _OMIT else sanitized)
        if len(value) > WS_REPLAY_MAX_LIST_ITEMS:
            truncated_fields.append(f"{path}[{WS_REPLAY_MAX_LIST_ITEMS}:]")
            result.append(WS_REPLAY_LIST_TRUNCATED)
        return result

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        items = list(value.items())
        for key, item in items[:WS_REPLAY_MAX_DICT_ITEMS]:
            key_text = str(key)
            item_path = f"{path}.{key_text}" if path else key_text
            sanitized = _sanitize_replay_value(
                item,
                item_path,
                omitted_fields=omitted_fields,
                truncated_fields=truncated_fields,
                omit_dict_values=True,
            )
            if sanitized is _OMIT:
                continue
            result[key_text] = sanitized
        if len(items) > WS_REPLAY_MAX_DICT_ITEMS:
            truncated_fields.append(f"{path}.*" if path else "*")
            result["__replay_truncated__"] = WS_REPLAY_DICT_TRUNCATED
        return result

    return value


def sanitize_ws_replay_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a replay-log-safe copy of a websocket event payload.

    Live websocket delivery may include large binary/data-url fields. The replay
    log is long-lived JSONL, so it stores enough metadata to recover ordering and
    UI state without retaining unbounded binary strings or huge tool output.
    """

    omitted_fields: list[str] = []
    truncated_fields: list[str] = []
    root = dict(payload)

    if root.get("type") == "image_chunk" and isinstance(root.get("image_data"), str):
        image_data = str(root.pop("image_data"))
        omitted_fields.append("image_data")
        root["image_data_omitted"] = True
        root["image_data_size"] = len(image_data)

    sanitized = _sanitize_replay_value(
        root,
        "",
        omitted_fields=omitted_fields,
        truncated_fields=truncated_fields,
        omit_dict_values=True,
    )
    if not isinstance(sanitized, dict):
        sanitized = dict(payload)

    existing_omitted = sanitized.get("replay_omitted_fields")
    if isinstance(existing_omitted, list):
        omitted_fields.extend(str(item) for item in existing_omitted)
    existing_truncated = sanitized.get("replay_truncated_fields")
    if isinstance(existing_truncated, list):
        truncated_fields.extend(str(item) for item in existing_truncated)

    if omitted_fields:
        sanitized["replay_omitted_fields"] = sorted(set(omitted_fields))
    if truncated_fields:
        sanitized["replay_truncated_fields"] = sorted(set(truncated_fields))
    return sanitized


class WebSocketReplayEventStore:
    """Small JSONL-backed replay log for conversation-scoped websocket events."""

    def __init__(self, *, session_id: str, root_dir: Path) -> None:
        self.session_id = session_id
        self.root_dir = Path(root_dir)
        safe_session_id = re.sub(r"[^A-Za-z0-9_-]+", "_", session_id).strip("_") or "session"
        self.path = self.root_dir / f"{safe_session_id}.jsonl"

    def load(self, *, limit: int) -> list[dict[str, Any]]:
        if limit <= 0 or not self.path.exists():
            return []

        events: list[dict[str, Any]] = []
        should_rewrite = False
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.debug("Skipping malformed websocket replay log line in %s", self.path)
                        continue
                    if isinstance(payload, dict):
                        sanitized = sanitize_ws_replay_payload(payload)
                        if sanitized != payload:
                            should_rewrite = True
                        events.append(sanitized)
        except OSError as exc:
            logger.debug("Failed to load websocket replay log for %s: %s", self.session_id, exc)
            return []

        limited = events[-limit:]
        if len(events) > limit:
            should_rewrite = True
        if should_rewrite:
            try:
                self.rewrite(limited)
            except OSError as exc:
                logger.debug("Failed to sanitize websocket replay log for %s: %s", self.session_id, exc)

        return limited

    def append(self, payload: dict[str, Any]) -> None:
        self.root_dir.mkdir(parents=True, exist_ok=True)
        safe_payload = sanitize_ws_replay_payload(payload)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")

    def rewrite(self, events: list[dict[str, Any]]) -> None:
        self.root_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            for payload in events:
                safe_payload = sanitize_ws_replay_payload(payload)
                handle.write(json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":")))
                handle.write("\n")
        tmp_path.replace(self.path)
