from __future__ import annotations

import json
from pathlib import Path

from backend.services.conversation_goal_service import build_goal_updated_payload
from backend.services.goal_runtime import GoalTurnAccounting, apply_budget, goal_response, new_goal
from backend.conversations.models import utc_now_iso
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.contracts import ToolSpec


class GoalTool(BaseTool):
    should_defer = False
    permission = PermissionLevel.AUTO
    read_only = False
    mutates_workspace = False
    idempotent = False
    result_kind = "status"

    def __init__(self, name, config):
        self.name = name
        effective = config.config_layer_stack.effective_config() if config.config_layer_stack is not None else {}
        self.max_budget = effective.get("goals", {}).get("max_goal_token_budget")
        self.description = {
            "get_goal": "Get the current goal for this thread, including status, budgets, token and elapsed-time usage, and remaining token budget.",
            "create_goal": "Create a goal only when explicitly requested by the user or system/developer instructions; do not infer goals from ordinary tasks. Set token_budget only when an explicit token budget is requested. Fails if an unfinished goal exists; use update_goal only for status.",
            "update_goal": Path(__file__).with_name("goal_update_instructions.txt").read_text(encoding="utf-8"),
        }[name]

    def is_concurrency_safe(self, args=None):
        # Goal mutations are local conversation state. Ordering belongs to
        # this batching contract, independently of filesystem/external risk.
        return False

    def get_spec(self):
        required = {"create_goal": ("objective",), "update_goal": ("status",)}.get(self.name, ())
        return ToolSpec(name=self.name, capability="agent.goal", toolset="core", exposure="core", required_args=required)

    def get_schema(self):
        properties = {"create_goal": {"objective": {"type": "string", "minLength": 1, "maxLength": 4000},
            "token_budget": {"type": "integer", "minimum": 1}},
            "update_goal": {"status": {"type": "string", "enum": ["complete", "blocked", "paused"]}}}.get(self.name, {})
        return ToolSchema(name=self.name, description=self.description, parameters={"type": "object", "properties": properties,
            "required": list(self.get_spec().required_args), "additionalProperties": False})

    async def execute(self, args, context=None):
        owner = context.run_context if context is not None else None
        if owner is None or owner.conversation_repository is None or not context.conversation_id:
            return self._error_result("Goal tools require a persistent conversation")
        if owner.goal_accounting is None:
            owner.goal_accounting = GoalTurnAccounting(owner.conversation_repository, context.conversation_id)
        accounting = owner.goal_accounting
        accounting.account()
        repository = owner.conversation_repository
        if self.name == "create_goal":
            objective = args["objective"].strip()
            budget = args.get("token_budget", self.max_budget)
            if not objective:
                return self._error_result("objective must not be blank")
            if self.max_budget is not None and budget is not None and budget > self.max_budget:
                return self._error_result(f"token_budget exceeds the configured maximum {self.max_budget}")
            def create(current):
                if current and current.get("status") != "complete":
                    raise ValueError("An unfinished goal exists; complete it before creating another")
                return new_goal(objective, token_budget=budget)
            try:
                record = repository.mutate_goal(context.conversation_id, create)
            except ValueError as error:
                return self._error_result(str(error))
            accounting.mark_active(record.goal)
        elif self.name == "update_goal":
            def update(current):
                if not current:
                    raise ValueError("No goal exists for this conversation")
                apply_budget(current)
                if current["status"] not in {"budget_limited", "usage_limited"}:
                    current.update(status=args["status"], updated_at=utc_now_iso(), _continuation_pending=False)
                return current
            try:
                record = repository.mutate_goal(context.conversation_id, update)
            except ValueError as error:
                return self._error_result(str(error))
        else:
            record = repository.get_conversation(context.conversation_id)
        if context.emit_event is not None:
            await context.emit_event("goal.updated", {key: value for key, value in build_goal_updated_payload(
                conversation_id=context.conversation_id, goal=record.goal, source=self.name, revision=record.revision).items() if key != "type"})
        response = goal_response(record.goal, context.conversation_id)
        if self.name == "update_goal" and record.goal["status"] == "complete" and record.goal.get("token_budget") is not None:
            response["completion_budget_report"] = {"tokens_used": record.goal["tokens_used"], "token_budget": record.goal["token_budget"]}
        return ToolResult(content=json.dumps(response), result_kind="status")
