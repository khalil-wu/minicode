from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
import time

from backend.conversations.models import utc_now_iso
from backend.services.runtime_control_service import CommandOutcome
from backend.services.command_target import resolve_conversation_target as resolve_goal_target
from backend.services.goal_runtime import account_elapsed, apply_budget, new_goal

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
        if current_goal.get("status") == "active":
            account_elapsed(next_goal)
        if action == "resume":
            next_goal.update(_accounted_at=time.time(), _empty_turns=0, _execution_failures=0, _continuation_pending=True)
        else:
            next_goal["_continuation_pending"] = False
        apply_budget(next_goal)
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

    if len(text) > 4000:
        return GoalAction(False, current_goal, "none", {}, CommandOutcome(
            "goal", "Goal exceeds the 4000-character limit.", level="error",
            data={"conversation_id": conversation_id, "goal": current_goal},
        ))
    budget = data.get("token_budget", current_goal.get("token_budget"))
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, int) or budget < 1):
        return GoalAction(False, current_goal, "none", {}, CommandOutcome("goal", "token_budget must be a positive integer", level="error"))
    now = utc_now_iso()
    next_goal = {
        **(current_goal if current_goal and current_goal.get("status") != "complete" else new_goal(text, token_budget=budget, source=source)),
        "text": text,
        "status": "active",
        "updated_at": now,
        "source": source,
        "token_budget": budget,
        "_continuation_pending": True,
        "_empty_turns": 0,
        "_execution_failures": 0,
    }
    if current_goal.get("status") != "active":
        next_goal["_accounted_at"] = time.time()
    apply_budget(next_goal)
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
