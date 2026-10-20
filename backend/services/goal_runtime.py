"""Persisted goals, charged from the real provider usage and existing turn owner."""
from __future__ import annotations

import time
from html import escape
from pathlib import Path
from uuid import uuid4

from backend.conversations.models import utc_now_iso


def new_goal(objective, *, token_budget=None, source="model"):
    now = utc_now_iso()
    return {"id": uuid4().hex, "text": objective, "status": "active", "token_budget": token_budget,
        "tokens_used": 0, "time_used_seconds": 0, "created_at": now, "updated_at": now,
        "source": source, "_accounted_at": time.time(), "_continuation_pending": True,
        "_empty_turns": 0, "_execution_failures": 0}


def goal_response(goal, conversation_id):
    if not goal:
        return {"goal": None, "remaining_tokens": None}
    budget = goal.get("token_budget")
    return {"goal": {"thread_id": conversation_id, "objective": goal["text"], "status": goal["status"],
        **{key: goal.get(key) for key in ("token_budget", "tokens_used", "time_used_seconds", "created_at", "updated_at")}},
        "remaining_tokens": max(0, budget - goal.get("tokens_used", 0)) if budget is not None else None}


def account_elapsed(goal):
    now = time.time()
    seconds = max(0, int(now - goal.get("_accounted_at", now)))
    goal["time_used_seconds"] = goal.get("time_used_seconds", 0) + seconds
    goal["_accounted_at"] = goal.get("_accounted_at", now) + seconds


def apply_budget(goal):
    if goal.get("token_budget") is not None and goal.get("tokens_used", 0) >= goal["token_budget"]:
        goal.update(status="budget_limited", _continuation_pending=False)


def _render_goal_prompt(goal, template_name, *, unbounded_remaining):
    template = Path(__file__).with_name(template_name).read_text(encoding="utf-8")
    values = {"objective": escape(goal["text"], quote=False), "tokens_used": goal.get("tokens_used", 0),
        "token_budget": goal.get("token_budget") if goal.get("token_budget") is not None else "none",
        "remaining_tokens": max(0, goal["token_budget"] - goal.get("tokens_used", 0)) if goal.get("token_budget") is not None else unbounded_remaining}
    for name, value in values.items():
        template = template.replace("{{ " + name + " }}", str(value))
    return template


def continuation_prompt(goal):
    return _render_goal_prompt(goal, "goal_continuation.md", unbounded_remaining="unbounded")


def objective_updated_prompt(goal):
    return _render_goal_prompt(goal, "goal_objective_updated.md", unbounded_remaining="unknown")


class GoalTurnAccounting:
    def __init__(self, repository, conversation_id, *, automatic=False):
        self.repository = repository
        self.conversation_id = conversation_id
        self.automatic = automatic
        self.goal_id = ""
        self.usage = {}
        self.accounted = {}
        self.finished = False
        self.pending_steering = []
        self.pending_user_updates = []
        self.revision = None
        record = repository.get_conversation(conversation_id)
        if record is not None and record.goal.get("status") == "active":
            self.goal_id = record.goal["id"]
            repository.mutate_goal(conversation_id, lambda goal: {**goal, "_continuation_pending": False})

    def register_usage(self, run_id, usage):
        self.usage[run_id] = usage
        self.accounted[run_id] = usage.billable_tokens

    def mark_active(self, goal):
        self.goal_id = goal["id"]
        self.accounted = {run_id: usage.billable_tokens for run_id, usage in self.usage.items()}

    def apply_external_update(self, goal, previous_goal):
        if goal.get("status") == "active":
            if previous_goal.get("status") != "active" or previous_goal.get("id") != goal["id"]:
                self.mark_active(goal)
            if previous_goal.get("text") != goal["text"]:
                self.pending_steering.append(objective_updated_prompt(goal))
        else:
            # Source pauses goal accounting and automatic continuation while
            # allowing the already admitted turn to finish.
            self.goal_id = ""

    def take_steering(self):
        pending, self.pending_steering = self.pending_steering, []
        return pending

    def take_user_updates(self):
        pending, self.pending_user_updates = self.pending_user_updates, []
        return pending

    def account(self):
        if not self.goal_id:
            return None
        totals = {run_id: usage.billable_tokens for run_id, usage in self.usage.items()}
        delta = sum(total - self.accounted.get(run_id, 0) for run_id, total in totals.items())
        def mutate(goal):
            if goal.get("id") != self.goal_id or goal.get("status") != "active":
                return goal
            account_elapsed(goal)
            goal["tokens_used"] = goal.get("tokens_used", 0) + delta
            goal["updated_at"] = utc_now_iso()
            apply_budget(goal)
            return goal
        record = self.repository.mutate_goal(self.conversation_id, mutate)
        self.revision = record.revision if record is not None else None
        self.accounted = totals
        return record.goal if record is not None else None

    def finish(self, *, status, content="", has_activity=False, reason=""):
        self.account()
        def mutate(goal):
            if goal.get("id") != self.goal_id or goal.get("status") != "active":
                return goal
            empty = self.automatic and not content.strip() and not has_activity and status == "completed"
            goal["_empty_turns"] = goal.get("_empty_turns", 0) + 1 if empty else 0
            if status in {"completed", "partial"}:
                goal["_execution_failures"] = 0
            if status == "failed":
                goal["status"] = "usage_limited" if reason in {"usage_limit", "usage_limit_exceeded"} else "blocked"
            elif goal["_empty_turns"] >= 3:
                goal["status"] = "blocked"
            goal["_continuation_pending"] = status in {"completed", "partial"} and goal["status"] == "active"
            goal["updated_at"] = utc_now_iso()
            return goal
        record = self.repository.mutate_goal(self.conversation_id, mutate) if self.goal_id else None
        self.revision = record.revision if record is not None else None
        self.finished = True
        return record.goal if record is not None else None

    def execution_unavailable(self):
        def mutate(goal):
            if goal.get("id") != self.goal_id or goal.get("status") != "active":
                return goal
            goal["_execution_failures"] = goal.get("_execution_failures", 0) + 1
            if goal["_execution_failures"] >= 3:
                goal["status"] = "blocked"
            goal["_continuation_pending"] = goal["status"] == "active"
            goal["updated_at"] = utc_now_iso()
            return goal
        record = self.repository.mutate_goal(self.conversation_id, mutate) if self.goal_id else None
        self.revision = record.revision if record is not None else None
        self.finished = True
        return record.goal if record is not None else None
