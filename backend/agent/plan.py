"""Legacy plan-state helpers.

Claude-style plan mode is enforced by permissions in the main ReAct loop. This
module only supports displaying imported or legacy plan.update payloads; it must
not be used as an automatic plan/accept/execute orchestrator.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

from backend.agent.planner import ExecutionPlan, build_execution_plan
from backend.llm.base import LLMAdapter

PlanStatus = Literal["draft", "accepted", "executing", "completed", "cancelled"]
StepStatus = Literal["pending", "running", "done", "skipped", "failed"]


@dataclass
class PlanStepState:
    id: str
    title: str
    detail: str
    status: StepStatus = "pending"

    def to_payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "detail": self.detail,
            "status": self.status,
        }


@dataclass
class PlanRecord:
    plan_id: str
    status: PlanStatus
    summary: str
    steps: list[PlanStepState] = field(default_factory=list)
    current_step: int = 0

    def to_payload(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "status": self.status,
            "steps": [s.to_payload() for s in self.steps],
            "current_step": self.current_step,
            "note": self.summary,
        }


def _from_execution_plan(plan: ExecutionPlan) -> PlanRecord:
    return PlanRecord(
        plan_id=f"plan-{uuid4().hex[:8]}",
        status="draft",
        summary=plan.summary,
        steps=[
            PlanStepState(id=f"s{i}", title=s.title, detail=s.instruction)
            for i, s in enumerate(plan.steps)
        ],
    )


async def generate_plan(llm: LLMAdapter, user_message: str) -> PlanRecord:
    plan = await build_execution_plan(llm, user_message)
    return _from_execution_plan(plan)


@dataclass
class PlanRegistry:
    """In-process registry for legacy plan.update display state."""

    _plans: dict[str, PlanRecord] = field(default_factory=dict)
    _accepts: dict[str, asyncio.Event] = field(default_factory=dict)

    def register(self, plan: PlanRecord) -> asyncio.Event:
        self._plans[plan.plan_id] = plan
        ev = asyncio.Event()
        self._accepts[plan.plan_id] = ev
        return ev

    def get(self, plan_id: str) -> PlanRecord | None:
        return self._plans.get(plan_id)

    def edit(
        self,
        plan_id: str,
        steps: list[dict[str, object]] | None,
        accept: bool,
    ) -> PlanRecord | None:
        plan = self._plans.get(plan_id)
        if plan is None:
            return None
        if steps:
            plan.steps = [
                PlanStepState(
                    id=str(s.get("id") or f"s{i}"),
                    title=str(s.get("title") or ""),
                    detail=str(s.get("detail") or ""),
                    status=str(s.get("status") or "pending"),  # type: ignore[arg-type]
                )
                for i, s in enumerate(steps)
            ]
        if accept:
            plan.status = "accepted"
            ev = self._accepts.get(plan_id)
            if ev:
                ev.set()
        return plan

    def cancel(self, plan_id: str) -> None:
        plan = self._plans.get(plan_id)
        if plan:
            plan.status = "cancelled"
        ev = self._accepts.get(plan_id)
        if ev:
            ev.set()


PLAN_REGISTRY = PlanRegistry()
