from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from backend.conversations.models import utc_now_iso
from backend.services.runtime_control_service import CommandOutcome

GoalEventScope = Literal["none", "active", "always"]


@dataclass(frozen=True)
class GoalAction:
    should_update: bool
    next_goal: dict[str, Any]
    event_scope: GoalEventScope
    event_goal: dict[str, Any]
    outcome: CommandOutcome



def resolve_goal_target(conversation_repo: Any, data: dict[str, Any], *, active_conversation_id: str = "") -> tuple[str, Any | None]:
    explicit_conversation_id = str(data.get("conversation_id") or "").strip()
    conversation_id = str(explicit_conversation_id or active_conversation_id or "").strip()
    if not conversation_id:
        return "", None
    return conversation_id, conversation_repo.get_conversation(conversation_id)


def build_goal_updated_payload(
    *,
    conversation_id: str,
    goal: dict[str, Any],
    source: str,
) -> dict[str, Any]:
    return {
        "type": "goal.updated",
        "conversation_id": conversation_id,
        "goal": dict(goal or {}),
        "source": source,
    }


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
