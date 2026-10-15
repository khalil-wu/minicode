"""Source GuardianApprovedAction framing from an owned, previously denied event."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

MANUAL_APPROVAL_DEVELOPER_PREFIX = "The user has manually approved a specific action that was previously `Rejected`."


def parse_manual_approval_command(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    thread_id = data.get("threadId")
    event = data.get("event")
    if not isinstance(thread_id, str) or not thread_id or not isinstance(event, dict) or not isinstance(event.get("id"), str):
        raise ValueError("The command requires a conversation and a selected Guardian denial event")
    return thread_id, event


@dataclass(frozen=True)
class ManualApprovalReceipt:
    review_id: str
    conversation_id: str
    turn_id: str
    environment_id: str | None
    request_digest: str
    policy_identity: str
    action_json: str
    approved_action_json: str
    developer_body: str
    history_reset_version: int

    @classmethod
    def from_denial(cls, record: dict[str, Any]) -> ManualApprovalReceipt:
        assessment = record["assessment"]
        if assessment["status"] != "denied":
            raise ValueError("Only a denied Guardian action can be manually approved")
        approved_action_json = json.dumps({"action": assessment["action"], "outcome": "allowed"},
            ensure_ascii=False, indent=2, sort_keys=True)
        body = (
            f"{MANUAL_APPROVAL_DEVELOPER_PREFIX}\n\n"
            "Treat this as approval to perform that exact action in the same context in which it was originally requested.\n"
            "Do not assume this also authorizes similar operations with different payloads.\n\n"
            f"Approved action:\n{approved_action_json}"
        )
        return cls(record["review_id"], record["conversation_id"], record["turn_id"], record["environment_id"],
            record["request_digest"], record["policy_identity"], record["action_json"], approved_action_json,
            body, record["history_reset_version"])


def require_owned_denial(event: dict[str, Any], record: dict[str, Any], conversation_id: str) -> None:
    """A client selects its real host-owned event; its payload cannot replace it."""
    if record["conversation_id"] != conversation_id:
        raise ValueError("The Guardian denial belongs to a different conversation")
    assessment = record["assessment"]
    if event.get("id") != record["review_id"] or event.get("status") != "denied" or assessment["status"] != "denied":
        raise ValueError("The selected event is not an owned Guardian denial")
    for key in ("turn_id", "target_item_id", "started_at_ms", "action"):
        if event.get(key) != assessment.get(key):
            raise ValueError("The selected Guardian action no longer matches its recorded denial")
