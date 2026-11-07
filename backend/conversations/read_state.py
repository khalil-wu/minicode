"""Durable thread attention receipts, independent of history checkpoints."""
from __future__ import annotations

from copy import deepcopy
from uuid import uuid4


class ReadStateConflict(ValueError):
    def __init__(self, read_state: dict, metadata_revision: int):
        super().__init__("Read state changed; review the current result before marking it again.")
        self.read_state = deepcopy(read_state)
        self.metadata_revision = metadata_revision


def initial_read_state(conversation_id: str, unread: bool = False) -> dict:
    return {"firstUnread": {"type": "threadStart"} if unread else None,
            "revision": f"initial:{conversation_id}"}


def bind_read_receipt(record, receipt: dict | None) -> None:
    record.read_state = (deepcopy(receipt["readState"]) if receipt is not None
                         else initial_read_state(record.id, record.has_unread_turn))
    record.last_published_turn = receipt["lastPublishedTurn"] if receipt is not None else ""
    record.has_unread_turn = record.read_state["firstUnread"] is not None


def stored_read_receipt(record) -> dict | None:
    state = record.read_state
    if not state or state["revision"] == f"initial:{record.id}":
        return None
    return {"readState": deepcopy(state), "lastPublishedTurn": record.last_published_turn}


def mark_read_state(record, unread: bool, *, expected_revision: str) -> None:
    state = record.read_state or initial_read_state(record.id, record.has_unread_turn)
    if state["revision"] != expected_revision:
        raise ReadStateConflict(state, record.revision)
    record.read_state = {"firstUnread": {"type": "threadStart"} if unread else None,
                         "revision": str(uuid4())}
    record.has_unread_turn = unread


def publish_turn_attention(record, message: dict) -> bool:
    notification = message.get("metadata", {}).get("completion_notification")
    if notification is None:
        return False
    status = message.get("terminal_status")
    text = message.get("content", "")
    if record.thread_source == "user" and record.conversation_type == "main":
        has_error = status == "failed" or bool(message.get("failure_message"))
        if not has_error and (not notification["should_mark_unread"] or not text.strip() or "<decision>DONT_NOTIFY</decision>" in text
                              or record.goal.get("status") == "active"):
            return False
    elif not notification["should_mark_unread"]:
        return False
    turn_id = str(message.get("turn_id") or message["id"])
    if turn_id == record.last_published_turn:
        return False
    previous = record.read_state or initial_read_state(record.id, record.has_unread_turn)
    record.read_state = {"firstUnread": previous["firstUnread"] or {"type": "turn", "turnId": turn_id},
                         "revision": str(uuid4())}
    record.last_published_turn = turn_id
    record.has_unread_turn = True
    return True


def retire_removed_unread_turn(record, retained_transcript: list[dict], *, expected_revision: str) -> None:
    state = record.read_state
    if not state or state["revision"] != expected_revision:
        return
    position = state["firstUnread"]
    if position is None or position["type"] != "turn":
        return
    turn_id = position["turnId"]
    had_turn = any(str(row.get("turn_id") or row.get("id")) == turn_id for row in record.transcript)
    keeps_turn = any(str(row.get("turn_id") or row.get("id")) == turn_id for row in retained_transcript)
    if had_turn and not keeps_turn:
        mark_read_state(record, False, expected_revision=expected_revision)
