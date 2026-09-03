from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from backend.conversations.models import utc_now_iso
from backend.services.runtime_control_service import CommandOutcome
from backend.services.command_target import resolve_conversation_target as resolve_goal_target

GoalEventScope = Literal["none", "active", "always"]


@dataclass(frozen=True)
class GoalAction:
    should_update: bool
    next_goal: dict[str, Any]
    event_scope: GoalEventScope
    event_goal: dict[str, Any]
    outcome: CommandOutcome



def build_goal_updated_payload(
    *,
    conversation_id: str,
    goal: dict[str, Any],
    source: str,
    updated_at: str = "",
    revision: int | None = None,
) -> dict[str, Any]:
    payload = {
        "type": "goal.updated",
        "conversation_id": conversation_id,
        "goal": dict(goal or {}),
        "source": source,
    }
    if str(updated_at or "").strip():
        payload["updated_at"] = str(updated_at).strip()
    if revision is not None:
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 0
            or revision > 9_007_199_254_740_991
        ):
            raise ValueError("revision must be a non-negative integer")
        payload["revision"] = revision
    return payload


def prepare_goal_action(
    data: dict[str, Any],
    *,
    conversation_id: str,
    current_goal: dict[str, Any],
    source: str,
) -> GoalAction:
    action = str(data.get("action") or "set").strip().lower()

    if action in {"show", "status", "inspect"}:
        if current_goal.get("text"):
            outcome = CommandOutcome(
                "goal",
                f"Goal is {current_goal.get('status', 'active')}: {current_goal.get('text')}",
                data={"conversation_id": conversation_id, "goal": current_goal},
            )
        else:
            outcome = CommandOutcome(
                "goal",
                "No goal is set for this conversation.",
                data={"conversation_id": conversation_id, "goal": {}},
            )
        return GoalAction(False, current_goal, "always", current_goal, outcome)

    if action in {"clear", "delete", "reset"}:
        return GoalAction(
            True,
            {},
            "active",
            {},
            CommandOutcome(
                "goal",
                "Cleared the conversation goal.",
                data={"conversation_id": conversation_id, "goal": {}},
            ),
        )

    if action in {"pause", "resume"}:
        if not current_goal.get("text"):
            return GoalAction(
                False,
                current_goal,
                "none",
                {},
                CommandOutcome(
                    "goal",
                    "No goal is set for this conversation.",
                    level="warning",
                    data={"conversation_id": conversation_id, "goal": {}},
                ),
            )
        next_goal = {
            **current_goal,
            "status": "paused" if action == "pause" else "active",
            "updated_at": utc_now_iso(),
            "source": source,
        }
        return GoalAction(
            True,
            next_goal,
            "active",
            next_goal,
            CommandOutcome(
                "goal",
                f"Goal {next_goal['status']}.",
                data={"conversation_id": conversation_id, "goal": next_goal},
            ),
        )

    text = str(data.get("text") or data.get("goal") or "").strip()
    if not text:
        return GoalAction(
            False,
            current_goal,
            "none",
            {},
            CommandOutcome(
                "goal",
                "Usage: /goal <target> | /goal pause | /goal resume | /goal clear",
                level="warning",
            ),
        )

    now = utc_now_iso()
    next_goal = {
        "id": str(current_goal.get("id") or f"goal_{conversation_id}"),
        "text": text[:4000],
        "status": "active",
        "created_at": str(current_goal.get("created_at") or now),
        "updated_at": now,
        "source": source,
    }
    return GoalAction(
        True,
        next_goal,
        "active",
        next_goal,
        CommandOutcome(
            "goal",
            "Updated the conversation goal.",
            data={"conversation_id": conversation_id, "goal": next_goal},
        ),
    )
