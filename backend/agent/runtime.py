"""Runtime records and metrics for MiniCode's agent control plane.

This module is deliberately small: the existing ReAct loop remains the
execution kernel, while AgentRuntime gives WebSocket/UI layers one stable
shape for runs, phases, subagents, checkpoints, and local observability.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from backend.config import PROJECT_ROOT, TokenBudget

AgentRunStatus = Literal["running", "completed", "failed", "cancelled"]
AgentRunPhase = Literal["plan", "execute", "verify", "recover", "final"]

METRICS_DIR = PROJECT_ROOT / "data" / "metrics"
METRICS_FILE = METRICS_DIR / "agent_metrics.jsonl"


def epoch_ms() -> int:
    return int(time.time() * 1000)


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


@dataclass
class AgentRunRecord:
    run_id: str
    conversation_id: str = ""
    parent_run_id: str = ""
    role: str = "main"
    phase: AgentRunPhase = "plan"
    status: AgentRunStatus = "running"
    budget: dict[str, Any] = field(default_factory=dict)
    started_at: int = field(default_factory=epoch_ms)
    completed_at: int | None = None
    task_id: str = ""
    session_id: str = ""
    summary: str = ""
    error: str = ""
    display_scope: str = "activity"
    panel_hint: str = "plan"
    requires_attention: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def with_phase(self, phase: AgentRunPhase, *, summary: str = "") -> "AgentRunRecord":
        self.phase = phase
        if summary:
            self.summary = summary
        return self

    def complete(self, status: AgentRunStatus = "completed", *, summary: str = "", error: str = "") -> "AgentRunRecord":
        self.status = status
        self.phase = "final"
        self.completed_at = epoch_ms()
        if summary:
            self.summary = summary
        if error:
            self.error = error
        self.requires_attention = status in {"failed", "cancelled"} or bool(error)
        return self


@dataclass
class SubagentRunRecord:
    subagent_id: str
    parent_run_id: str = ""
    agent_type: str = "general-purpose"
    prompt_summary: str = ""
    status: AgentRunStatus = "running"
    tool_count: int = 0
    result_summary: str = ""
    checkpoint_id: str = ""
    started_at: int = field(default_factory=epoch_ms)
    completed_at: int | None = None
    display_scope: str = "agents"
    panel_hint: str = "subagents"
    requires_attention: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def complete(self, status: AgentRunStatus = "completed", *, summary: str = "", tool_count: int = 0) -> "SubagentRunRecord":
        self.status = status
        self.completed_at = epoch_ms()
        if summary:
            self.result_summary = summary
        self.tool_count = tool_count
        self.requires_attention = status in {"failed", "cancelled"}
        return self


@dataclass
class RunCheckpoint:
    run_id: str
    session_id: str
    conversation_id: str = ""
    iteration: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    stopped_reason: str | None = None
    resume_payload: dict[str, Any] = field(default_factory=dict)
    created_at: int = field(default_factory=epoch_ms)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def budget_snapshot(budget: TokenBudget | None) -> dict[str, Any]:
    if budget is None:
        return {}
    return {
        "total": getattr(budget, "total", 0),
        "tool_schemas": getattr(budget, "tool_schemas", 0),
        "active_skills": getattr(budget, "active_skills", 0),
    }


class AgentRuntime:
    """Tracks agent runs and appends local JSONL metrics."""

    def __init__(self, *, metrics_file: Path | None = None) -> None:
        self._metrics_file = metrics_file or METRICS_FILE
        self._runs: dict[str, AgentRunRecord] = {}
        self._subagents: dict[str, SubagentRunRecord] = {}

    def start_run(
        self,
        *,
        conversation_id: str = "",
        parent_run_id: str = "",
        role: str = "main",
        task_id: str = "",
        session_id: str = "",
        budget: TokenBudget | None = None,
        run_id: str | None = None,
    ) -> AgentRunRecord:
        record = AgentRunRecord(
            run_id=run_id or new_run_id(),
            conversation_id=conversation_id,
            parent_run_id=parent_run_id,
            role=role,
            task_id=task_id,
            session_id=session_id,
            budget=budget_snapshot(budget),
        )
        self._runs[record.run_id] = record
        self.write_metric("run_started", record.to_dict())
        return record

    def update_phase(self, run_id: str, phase: AgentRunPhase, *, summary: str = "") -> AgentRunRecord | None:
        record = self._runs.get(run_id)
        if record is None:
            return None
        record.with_phase(phase, summary=summary)
        self.write_metric("phase_updated", record.to_dict())
        return record

    def complete_run(self, run_id: str, status: AgentRunStatus = "completed", *, summary: str = "", error: str = "") -> AgentRunRecord | None:
        record = self._runs.get(run_id)
        if record is None:
            return None
        record.complete(status, summary=summary, error=error)
        self.write_metric("run_completed", record.to_dict())
        return record

    def start_subagent(
        self,
        *,
        subagent_id: str,
        parent_run_id: str = "",
        agent_type: str,
        prompt_summary: str = "",
    ) -> SubagentRunRecord:
        record = SubagentRunRecord(
            subagent_id=subagent_id,
            parent_run_id=parent_run_id,
            agent_type=agent_type,
            prompt_summary=prompt_summary,
        )
        self._subagents[subagent_id] = record
        self.write_metric("subagent_started", record.to_dict())
        return record

    def complete_subagent(
        self,
        subagent_id: str,
        status: AgentRunStatus = "completed",
        *,
        summary: str = "",
        tool_count: int = 0,
    ) -> SubagentRunRecord | None:
        record = self._subagents.get(subagent_id)
        if record is None:
            return None
        record.complete(status, summary=summary, tool_count=tool_count)
        self.write_metric("subagent_completed", record.to_dict())
        return record

    def write_metric(self, event: str, payload: dict[str, Any]) -> None:
        try:
            self._metrics_file.parent.mkdir(parents=True, exist_ok=True)
            metric = {"ts": epoch_ms(), "event": event, **payload}
            with self._metrics_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(metric, ensure_ascii=False, default=str) + "\n")
        except Exception:
            # Metrics must never affect the agent run.
            return

    def list_runs(self, *, conversation_id: str = "", include_subagents: bool = False) -> dict[str, Any]:
        """Return a lightweight runtime snapshot for UI/debug panels."""
        runs = [
            record.to_dict()
            for record in self._runs.values()
            if not conversation_id or record.conversation_id == conversation_id
        ]
        payload: dict[str, Any] = {"runs": runs}
        if include_subagents:
            parent_ids = {str(record.get("run_id") or "") for record in runs}
            payload["subagents"] = [
                record.to_dict()
                for record in self._subagents.values()
                if not conversation_id or record.parent_run_id in parent_ids
            ]
        return payload


_DEFAULT_RUNTIME = AgentRuntime()


def default_runtime() -> AgentRuntime:
    return _DEFAULT_RUNTIME
