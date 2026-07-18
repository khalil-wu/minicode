"""Runtime records and metrics for MiniCode's agent control plane.

This module is deliberately small: the existing ReAct loop remains the
execution kernel, while AgentRuntime gives WebSocket/UI layers one stable
shape for runs, phases, subagents, checkpoints, and local observability.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from backend.config import DATA_ROOT, TokenBudget
from backend.agent.execution_journal import ExecutionJournal, load_agent_transcript
from backend.agent.parent_notification_outbox import (
    ParentNotification,
    ParentNotificationOutbox,
    enqueue_parent_notification,
    load_parent_outbox,
)
from backend.agent.swarm_store import FileSwarmStore
from backend.agent.workflow_coordinator import WorkflowCoordinator

AgentRunStatus = Literal["running", "completed", "partial", "failed", "cancelled", "interrupted"]
AgentRunPhase = Literal["plan", "execute", "verify", "recover", "final"]
SwarmTaskStatus = Literal["pending", "in_progress", "blocked", "completed", "cancelled"]
WorkflowLauncher = Callable[[list["SwarmTaskRecord"]], Awaitable[Any]]

# ---------------------------------------------------------------------------
# Explicit four-type Agent taxonomy (plan §11.2)
# ---------------------------------------------------------------------------

AgentRole = Literal["primary", "subagent", "side_query", "background"]

AGENT_ROLES: frozenset[str] = frozenset({"primary", "subagent", "side_query", "background"})

# Concurrency limits (plan §11.3).
# These are deliberate guard-rails, not performance knobs. Nesting is blocked
# structurally: subagents cannot call task/workflow (SUBAGENT_DENIED_TOOLS).
MAX_CONCURRENT_SUBAGENTS = int(os.environ.get("MINICODE_MAX_CONCURRENT_SUBAGENTS", "5"))

# Bound on retained subagent results per parent coordinator run. Results are
# append-only unless explicitly consumed (task_status consume=True), so without
# a cap a long coordinator session grows memory without limit and every
# delegation/task_status re-scans all of a parent's retained results for
# evidence conflicts. Older retained results are evicted (FIFO) past this cap.
MAX_RETAINED_SUBAGENT_RESULTS_PER_PARENT = int(
    os.environ.get("MINICODE_MAX_RETAINED_SUBAGENT_RESULTS_PER_PARENT", "24")
)

# Write-scope strategy: subagents may be confined to a git worktree so that
# their file mutations do not leak into the primary workspace until merged.
WRITE_SCOPE_STRATEGIES: frozenset[str] = frozenset({"none", "workspace", "worktree", "readonly"})

METRICS_DIR = DATA_ROOT / "metrics"
METRICS_FILE = METRICS_DIR / "agent_metrics.jsonl"
SWARM_DIR = DATA_ROOT / "swarm"
_RUNTIME_INSTANCE_ID = uuid4().hex


def epoch_ms() -> int:
    return int(time.time() * 1000)


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def _process_is_alive(process_id: int) -> bool:
    if process_id <= 0:
        return False
    if process_id == os.getpid():
        return True
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
            0x1000,
            False,
            process_id,
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
        return True
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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
    runtime_instance_id: str = ""
    runtime_process_id: int = 0

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
        self.requires_attention = status in {"partial", "failed", "cancelled", "interrupted"} or bool(error)
        return self


@dataclass
class SubagentRunRecord:
    subagent_id: str
    parent_run_id: str = ""
    agent_type: str = "general-purpose"
    role: AgentRole = "subagent"
    write_scope_strategy: str = "workspace"
    prompt_summary: str = ""
    background: bool = False
    workflow_id: str = ""
    workflow_name: str = ""
    workflow_mode: str = ""
    node_id: str = ""
    task_id: str = ""
    objective: str = ""
    depends_on: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    required_for_final: bool = True
    cancel_with_parent: bool = True
    detach_from_parent: bool = False
    read_only: bool = False
    write_scope: list[str] = field(default_factory=list)
    current_activity: str = ""
    status: AgentRunStatus = "running"
    tool_count: int = 0
    result_summary: str = ""
    checkpoint_id: str = ""
    started_at: int = field(default_factory=epoch_ms)
    completed_at: int | None = None
    display_scope: str = "agents"
    panel_hint: str = "subagents"
    requires_attention: bool = False
    runtime_instance_id: str = ""
    runtime_process_id: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def complete(self, status: AgentRunStatus = "completed", *, summary: str = "", tool_count: int = 0) -> "SubagentRunRecord":
        self.status = status
        self.completed_at = epoch_ms()
        if summary:
            self.result_summary = summary
        self.tool_count = tool_count
        self.requires_attention = status in {"partial", "failed", "cancelled", "interrupted"}
        return self


@dataclass
class SubagentResultRecord:
    subagent_id: str
    status: AgentRunStatus
    content: str = ""
    error: str = ""
    duration_ms: int = 0
    iterations: int = 0
    tool_call_count: int = 0
    timed_out: bool = False
    # Token usage rolled up from the child's terminal ``done`` event so the
    # coordinator can see delegation cost, not just wall-clock/tool counts.
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    completed_at: int = field(default_factory=epoch_ms)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SwarmMessageRecord:
    message_id: str
    sender_id: str
    recipient_id: str
    content: str
    conversation_id: str = ""
    team_name: str = ""
    task_id: str = ""
    created_at: int = field(default_factory=epoch_ms)
    seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SwarmTaskOutputRecord:
    output_id: str
    author_id: str
    content: str
    created_at: int = field(default_factory=epoch_ms)
    seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SwarmTaskRecord:
    task_id: str
    title: str
    description: str = ""
    assignee: str = ""
    conversation_id: str = ""
    workflow_id: str = ""
    workflow_name: str = ""
    workflow_mode: str = ""
    node_id: str = ""
    agent_type: str = "general-purpose"
    role: str = ""
    objective: str = ""
    required_for_final: bool = True
    read_only: bool = False
    write_scope: list[str] = field(default_factory=list)
    status: SwarmTaskStatus = "pending"
    priority: str = "normal"
    team_name: str = ""
    created_by: str = ""
    created_at: int = field(default_factory=epoch_ms)
    updated_at: int = field(default_factory=epoch_ms)
    completed_at: int | None = None
    blocks: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    outputs: list[SwarmTaskOutputRecord] = field(default_factory=list)
    seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["outputs"] = [output.to_dict() for output in self.outputs]
        return data

    def update(self, patch: dict[str, Any]) -> None:
        for key in (
            "title",
            "description",
            "assignee",
            "priority",
            "team_name",
            "workflow_id",
            "workflow_name",
            "workflow_mode",
            "node_id",
            "agent_type",
            "role",
            "objective",
        ):
            if key in patch:
                setattr(self, key, str(patch[key] or "").strip())
        if "required_for_final" in patch:
            self.required_for_final = bool(patch["required_for_final"])
        if "read_only" in patch:
            self.read_only = bool(patch["read_only"])
        if "write_scope" in patch:
            self.write_scope = _string_list(patch.get("write_scope"))
        if "status" in patch:
            status = str(patch["status"] or "").strip()
            if status in {"pending", "in_progress", "blocked", "completed", "cancelled"}:
                self.status = status  # type: ignore[assignment]
                self.completed_at = epoch_ms() if status in {"completed", "cancelled"} else None
        self.updated_at = epoch_ms()


@dataclass
class SwarmTeamMemberRecord:
    id: str
    role: str = ""
    agent_type: str = "general-purpose"
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SwarmTeamRecord:
    team_id: str
    team_name: str
    description: str = ""
    conversation_id: str = ""
    created_by: str = ""
    created_at: int = field(default_factory=epoch_ms)
    updated_at: int = field(default_factory=epoch_ms)
    members: list[SwarmTeamMemberRecord] = field(default_factory=list)
    seq: int = 0
    deleted_at: int | None = None
    deleted_seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["members"] = [member.to_dict() for member in self.members]
        return data


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


def _swarm_message_from_dict(data: dict[str, Any]) -> SwarmMessageRecord:
    return SwarmMessageRecord(
        message_id=str(data.get("message_id") or ""),
        sender_id=str(data.get("sender_id") or ""),
        recipient_id=str(data.get("recipient_id") or ""),
        content=str(data.get("content") or ""),
        conversation_id=str(data.get("conversation_id") or ""),
        team_name=str(data.get("team_name") or ""),
        task_id=str(data.get("task_id") or ""),
        created_at=int(data.get("created_at") or epoch_ms()),
        seq=int(data.get("seq") or 0),
    )


def _swarm_task_output_from_dict(data: dict[str, Any]) -> SwarmTaskOutputRecord:
    return SwarmTaskOutputRecord(
        output_id=str(data.get("output_id") or ""),
        author_id=str(data.get("author_id") or ""),
        content=str(data.get("content") or ""),
        created_at=int(data.get("created_at") or epoch_ms()),
        seq=int(data.get("seq") or 0),
    )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _swarm_task_from_dict(data: dict[str, Any]) -> SwarmTaskRecord:
    status = str(data.get("status") or "pending")
    if status not in {"pending", "in_progress", "blocked", "completed", "cancelled"}:
        status = "pending"
    outputs = [
        _swarm_task_output_from_dict(output)
        for output in data.get("outputs", [])
        if isinstance(output, dict)
    ]
    return SwarmTaskRecord(
        task_id=str(data.get("task_id") or ""),
        title=str(data.get("title") or ""),
        description=str(data.get("description") or ""),
        assignee=str(data.get("assignee") or ""),
        conversation_id=str(data.get("conversation_id") or ""),
        workflow_id=str(data.get("workflow_id") or ""),
        workflow_name=str(data.get("workflow_name") or ""),
        workflow_mode=str(data.get("workflow_mode") or ""),
        node_id=str(data.get("node_id") or ""),
        agent_type=str(data.get("agent_type") or "general-purpose") or "general-purpose",
        role=str(data.get("role") or ""),
        objective=str(data.get("objective") or ""),
        required_for_final=bool(data.get("required_for_final", True)),
        read_only=bool(data.get("read_only", False)),
        write_scope=_string_list(data.get("write_scope")),
        status=status,  # type: ignore[arg-type]
        priority=str(data.get("priority") or "normal"),
        team_name=str(data.get("team_name") or ""),
        created_by=str(data.get("created_by") or ""),
        created_at=int(data.get("created_at") or epoch_ms()),
        updated_at=int(data.get("updated_at") or epoch_ms()),
        completed_at=data.get("completed_at") if isinstance(data.get("completed_at"), int) else None,
        blocks=_string_list(data.get("blocks")),
        blocked_by=_string_list(data.get("blocked_by")),
        outputs=outputs,
        seq=int(data.get("seq") or 0),
    )


def _subagent_from_dict(data: dict[str, Any]) -> SubagentRunRecord:
    return SubagentRunRecord(
        subagent_id=str(data.get("subagent_id") or ""),
        parent_run_id=str(data.get("parent_run_id") or ""),
        agent_type=str(data.get("agent_type") or "general-purpose") or "general-purpose",
        prompt_summary=str(data.get("prompt_summary") or ""),
        background=bool(data.get("background", False)),
        workflow_id=str(data.get("workflow_id") or ""),
        workflow_name=str(data.get("workflow_name") or ""),
        workflow_mode=str(data.get("workflow_mode") or ""),
        node_id=str(data.get("node_id") or ""),
        task_id=str(data.get("task_id") or ""),
        objective=str(data.get("objective") or ""),
        depends_on=_string_list(data.get("depends_on")),
        blocked_by=_string_list(data.get("blocked_by")),
        required_for_final=bool(data.get("required_for_final", True)),
        cancel_with_parent=bool(
            data.get(
                "cancel_with_parent",
                not bool(data.get("detach_from_parent", False)),
            )
        ),
        detach_from_parent=bool(data.get("detach_from_parent", False)),
        read_only=bool(data.get("read_only", False)),
        write_scope=_string_list(data.get("write_scope")),
        current_activity=str(data.get("current_activity") or ""),
        status=str(data.get("status") or "running"),  # type: ignore[arg-type]
        tool_count=int(data.get("tool_count") or 0),
        result_summary=str(data.get("result_summary") or ""),
        checkpoint_id=str(data.get("checkpoint_id") or ""),
        started_at=int(data.get("started_at") or epoch_ms()),
        completed_at=data.get("completed_at") if isinstance(data.get("completed_at"), int) else None,
        display_scope=str(data.get("display_scope") or "agents"),
        panel_hint=str(data.get("panel_hint") or "subagents"),
        requires_attention=bool(data.get("requires_attention", False)),
        runtime_instance_id=str(data.get("runtime_instance_id") or ""),
        runtime_process_id=int(data.get("runtime_process_id") or 0),
    )


def _agent_run_from_dict(data: dict[str, Any]) -> AgentRunRecord:
    phase = str(data.get("phase") or "plan")
    if phase not in {"plan", "execute", "verify", "recover", "final"}:
        phase = "plan"
    status = str(data.get("status") or "running")
    if status not in {"running", "completed", "partial", "failed", "cancelled", "interrupted"}:
        status = "running"
    return AgentRunRecord(
        run_id=str(data.get("run_id") or ""),
        conversation_id=str(data.get("conversation_id") or ""),
        parent_run_id=str(data.get("parent_run_id") or ""),
        role=str(data.get("role") or "main"),
        phase=phase,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        budget=dict(data.get("budget") or {}),
        started_at=int(data.get("started_at") or epoch_ms()),
        completed_at=data.get("completed_at") if isinstance(data.get("completed_at"), int) else None,
        task_id=str(data.get("task_id") or ""),
        session_id=str(data.get("session_id") or ""),
        summary=str(data.get("summary") or ""),
        error=str(data.get("error") or ""),
        display_scope=str(data.get("display_scope") or "activity"),
        panel_hint=str(data.get("panel_hint") or "plan"),
        requires_attention=bool(data.get("requires_attention", False)),
        runtime_instance_id=str(data.get("runtime_instance_id") or ""),
        runtime_process_id=int(data.get("runtime_process_id") or 0),
    )


def _subagent_result_from_dict(data: dict[str, Any]) -> SubagentResultRecord:
    status = str(data.get("status") or "failed")
    if status not in {"running", "completed", "partial", "failed", "cancelled", "interrupted"}:
        status = "failed"
    return SubagentResultRecord(
        subagent_id=str(data.get("subagent_id") or ""),
        status=status,  # type: ignore[arg-type]
        content=str(data.get("content") or ""),
        error=str(data.get("error") or ""),
        duration_ms=int(data.get("duration_ms") or 0),
        iterations=int(data.get("iterations") or 0),
        tool_call_count=int(data.get("tool_call_count") or 0),
        timed_out=bool(data.get("timed_out", False)),
        input_tokens=int(data.get("input_tokens") or 0),
        output_tokens=int(data.get("output_tokens") or 0),
        total_tokens=int(data.get("total_tokens") or 0),
        completed_at=int(data.get("completed_at") or epoch_ms()),
    )


def _swarm_team_member_from_dict(data: dict[str, Any]) -> SwarmTeamMemberRecord:
    return SwarmTeamMemberRecord(
        id=str(data.get("id") or ""),
        role=str(data.get("role") or ""),
        agent_type=str(data.get("agent_type") or "general-purpose") or "general-purpose",
        description=str(data.get("description") or ""),
    )


def _swarm_team_from_dict(data: dict[str, Any]) -> SwarmTeamRecord:
    members = [
        _swarm_team_member_from_dict(member)
        for member in data.get("members", [])
        if isinstance(member, dict)
    ]
    return SwarmTeamRecord(
        team_id=str(data.get("team_id") or ""),
        team_name=str(data.get("team_name") or ""),
        description=str(data.get("description") or ""),
        conversation_id=str(data.get("conversation_id") or ""),
        created_by=str(data.get("created_by") or ""),
        created_at=int(data.get("created_at") or epoch_ms()),
        updated_at=int(data.get("updated_at") or epoch_ms()),
        members=members,
        seq=int(data.get("seq") or 0),
        deleted_at=data.get("deleted_at") if isinstance(data.get("deleted_at"), int) else None,
        deleted_seq=int(data.get("deleted_seq") or 0),
    )


class AgentRuntime:
    """Tracks agent runs and appends local JSONL metrics."""

    def __init__(
        self,
        *,
        metrics_file: Path | None = None,
        swarm_store_dir: Path | None = None,
        runtime_instance_id: str | None = None,
        runtime_process_id: int | None = None,
    ) -> None:
        self._metrics_file = metrics_file or METRICS_FILE
        self._runtime_instance_id = runtime_instance_id or _RUNTIME_INSTANCE_ID
        self._runtime_process_id = runtime_process_id or os.getpid()
        store_dir = swarm_store_dir or ((metrics_file.parent / "swarm") if metrics_file is not None else SWARM_DIR)
        self._swarm_store = FileSwarmStore(store_dir)
        # Keep sidechain journals / parent outboxes next to the swarm store so
        # isolated runtime fixtures and production share the same root layout.
        self._journal_root = store_dir.parent / "sidechains"
        self._outbox_root = store_dir.parent / "parent_notifications"
        self._runs: dict[str, AgentRunRecord] = {}
        self._subagents: dict[str, SubagentRunRecord] = {}
        self._subagent_tasks: dict[str, asyncio.Task[Any]] = {}
        self._subagent_slot_reservations: set[str] = set()
        self._subagent_cancel_events: dict[str, asyncio.Event] = {}
        self._subagent_completion_events: dict[str, asyncio.Event] = {}
        self._subagent_parent_run_ids: dict[str, str] = {}
        self._subagent_results: dict[str, SubagentResultRecord] = {}
        # name -> subagent_id registry for by-name addressing (SendMessage).
        # Latest-wins, mirroring cc's agentNameRegistry.
        self._subagent_name_registry: dict[str, str] = {}
        self._swarm_messages: dict[str, SwarmMessageRecord] = {}
        self._swarm_tasks: dict[str, SwarmTaskRecord] = {}
        self._swarm_teams: dict[str, SwarmTeamRecord] = {}
        self._workflow_coordinator = WorkflowCoordinator(max_launch_batch=MAX_CONCURRENT_SUBAGENTS)
        persisted = [
            *self._swarm_store.list_agent_runs(),
            *self._swarm_store.list_subagents(),
        ]
        active_process_ids = {
            process_id
            for item in persisted
            if (process_id := int(item.get("runtime_process_id") or 0))
            and _process_is_alive(process_id)
        }
        recovered = self._swarm_store.recover_runtime_state(
            interrupted_at=epoch_ms(),
            summary="Interrupted because the previous MiniCode process ended before completion.",
            current_instance_id=self._runtime_instance_id,
            active_process_ids=active_process_ids,
        )
        self._runs = {
            record.run_id: record
            for item in recovered["runs"]
            if (record := _agent_run_from_dict(item)).run_id
        }
        self._subagents = {
            record.subagent_id: record
            for item in recovered["subagents"]
            if (record := _subagent_from_dict(item)).subagent_id
        }
        self._subagent_results = {
            record.subagent_id: record
            for item in recovered["results"]
            if (record := _subagent_result_from_dict(item)).subagent_id
        }
        for record in self._subagents.values():
            if record.status != "interrupted" or not record.workflow_id or not record.task_id:
                continue
            task = self.get_swarm_task(record.task_id)
            if task is not None and task.status == "in_progress":
                self.update_swarm_task(record.task_id, {"status": "pending"})

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
            runtime_instance_id=self._runtime_instance_id,
            runtime_process_id=self._runtime_process_id,
        )
        self._runs[record.run_id] = record
        self._swarm_store.upsert_agent_run(record.to_dict())
        self.write_metric("run_started", record.to_dict())
        return record

    def update_phase(self, run_id: str, phase: AgentRunPhase, *, summary: str = "") -> AgentRunRecord | None:
        record = self._runs.get(run_id)
        if record is None:
            return None
        record.with_phase(phase, summary=summary)
        self._swarm_store.upsert_agent_run(record.to_dict())
        self.write_metric("phase_updated", record.to_dict())
        return record

    def complete_run(self, run_id: str, status: AgentRunStatus = "completed", *, summary: str = "", error: str = "") -> AgentRunRecord | None:
        record = self._runs.get(run_id)
        if record is None:
            return None
        record.complete(status, summary=summary, error=error)
        self._swarm_store.upsert_agent_run(record.to_dict())
        self.write_metric("run_completed", record.to_dict())
        return record

    def start_subagent(
        self,
        *,
        subagent_id: str,
        parent_run_id: str = "",
        agent_type: str,
        prompt_summary: str = "",
        background: bool = False,
        workflow_id: str = "",
        workflow_name: str = "",
        workflow_mode: str = "",
        node_id: str = "",
        task_id: str = "",
        objective: str = "",
        depends_on: list[str] | None = None,
        blocked_by: list[str] | None = None,
        required_for_final: bool = True,
        cancel_with_parent: bool | None = None,
        detach_from_parent: bool | None = None,
        read_only: bool = False,
        write_scope: list[str] | None = None,
        current_activity: str = "",
    ) -> SubagentRunRecord:
        existing = self._subagents.get(subagent_id)
        if subagent_id in self._subagent_slot_reservations:
            self._subagent_slot_reservations.discard(subagent_id)
        elif existing is None or existing.status != "running":
            active = sum(1 for item in self._subagents.values() if item.status == "running")
            if active + len(self._subagent_slot_reservations) >= MAX_CONCURRENT_SUBAGENTS:
                raise RuntimeError(
                    f"Maximum concurrent subagents reached ({MAX_CONCURRENT_SUBAGENTS})."
                )
        # Parent-child cancel semantics are independent of final-wait:
        # background + not required_for_final detaches by default (cc async unlinked
        # AbortController). Explicit flags always win.
        if detach_from_parent is None and cancel_with_parent is None:
            detach = bool(background and not required_for_final)
            cancel_linked = not detach
        elif detach_from_parent is not None:
            detach = bool(detach_from_parent)
            cancel_linked = (
                bool(cancel_with_parent)
                if cancel_with_parent is not None
                else (not detach)
            )
        else:
            cancel_linked = bool(cancel_with_parent)
            detach = not cancel_linked
        if detach:
            cancel_linked = False
        record = SubagentRunRecord(
            subagent_id=subagent_id,
            parent_run_id=parent_run_id,
            agent_type=agent_type,
            prompt_summary=prompt_summary,
            background=background,
            workflow_id=workflow_id,
            workflow_name=workflow_name,
            workflow_mode=workflow_mode,
            node_id=node_id,
            task_id=task_id,
            objective=objective,
            depends_on=depends_on or [],
            blocked_by=blocked_by or [],
            required_for_final=required_for_final,
            cancel_with_parent=cancel_linked,
            detach_from_parent=detach,
            read_only=read_only,
            write_scope=write_scope or [],
            current_activity=current_activity,
            runtime_instance_id=self._runtime_instance_id,
            runtime_process_id=self._runtime_process_id,
        )
        self._subagents[subagent_id] = record
        self._register_subagent_names(subagent_id, agent_type=agent_type, objective=objective, prompt_summary=prompt_summary)
        self._subagent_completion_events.setdefault(subagent_id, asyncio.Event())
        self._swarm_store.upsert_subagent(record.to_dict())
        self.write_metric("subagent_started", record.to_dict())
        return record

    def try_reserve_subagent_slots(self, subagent_ids: list[str]) -> bool:
        clean_ids = list(dict.fromkeys(str(value or "").strip() for value in subagent_ids if str(value or "").strip()))
        active = sum(1 for item in self._subagents.values() if item.status == "running")
        needed = sum(
            1
            for subagent_id in clean_ids
            if subagent_id not in self._subagent_slot_reservations
            and not (self._subagents.get(subagent_id) and self._subagents[subagent_id].status == "running")
        )
        if active + len(self._subagent_slot_reservations) + needed > MAX_CONCURRENT_SUBAGENTS:
            return False
        self._subagent_slot_reservations.update(clean_ids)
        return True

    def release_subagent_slot(self, subagent_id: str) -> None:
        self._subagent_slot_reservations.discard(str(subagent_id or "").strip())

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
        self._swarm_store.upsert_subagent(record.to_dict())
        self.write_metric("subagent_completed", record.to_dict())
        return record

    def register_subagent_task(
        self,
        subagent_id: str,
        task: asyncio.Task[Any],
        *,
        cancel_event: asyncio.Event | None = None,
        parent_run_id: str = "",
    ) -> None:
        self._subagent_tasks[subagent_id] = task
        self._subagent_completion_events.setdefault(subagent_id, asyncio.Event())
        if cancel_event is not None:
            self._subagent_cancel_events[subagent_id] = cancel_event
        parent = str(parent_run_id or "").strip()
        if parent:
            self._subagent_parent_run_ids[subagent_id] = parent
        self.write_metric("subagent_task_registered", {"subagent_id": subagent_id})

    def release_subagent_task(self, subagent_id: str) -> None:
        self._subagent_tasks.pop(subagent_id, None)
        self._subagent_cancel_events.pop(subagent_id, None)
        self._subagent_parent_run_ids.pop(subagent_id, None)

    async def wait_for_subagent(self, subagent_id: str, timeout: float) -> bool:
        """Wait for a result notification without polling runtime snapshots."""
        if subagent_id in self._subagent_results:
            return True
        event = self._subagent_completion_events.get(subagent_id)
        if event is None:
            return False
        try:
            await asyncio.wait_for(event.wait(), timeout=max(0.0, timeout))
        except asyncio.TimeoutError:
            return False
        return subagent_id in self._subagent_results

    def cancel_subagent_task(self, subagent_id: str) -> Literal["cancelled", "done", "not_found"]:
        task = self._subagent_tasks.get(subagent_id)
        if task is None:
            return "not_found"
        cancel_event = self._subagent_cancel_events.get(subagent_id)
        if cancel_event is not None:
            cancel_event.set()
        if task.done():
            self.release_subagent_task(subagent_id)
            return "done"
        task.cancel()
        self.write_metric("subagent_task_cancel_requested", {"subagent_id": subagent_id})
        return "cancelled"

    def cancel_child_subagent_tasks(self, parent_run_id: str, *, reason: str = "parent_cancelled") -> list[str]:
        parent = str(parent_run_id or "").strip()
        if not parent:
            return []
        cancelled: list[str] = []
        for subagent_id, recorded_parent in list(self._subagent_parent_run_ids.items()):
            if recorded_parent != parent:
                continue
            record = self._subagents.get(subagent_id)
            if record is not None:
                if bool(getattr(record, "detach_from_parent", False)):
                    continue
                if not bool(getattr(record, "cancel_with_parent", True)):
                    continue
            status = self.cancel_subagent_task(subagent_id)
            if status in {"cancelled", "done"}:
                cancelled.append(subagent_id)
        if cancelled:
            self.write_metric(
                "subagent_children_cancel_requested",
                {"parent_run_id": parent, "subagent_ids": cancelled, "reason": reason},
            )
        return cancelled

    def cancel_child_subagent_tasks_for_task(
        self,
        task_id: str,
        *,
        reason: str = "parent_cancelled",
    ) -> list[str]:
        task = str(task_id or "").strip()
        if not task:
            return []
        parent_run_ids = [
            run_id
            for run_id, record in self._runs.items()
            if str(getattr(record, "task_id", "") or "") == task
        ]
        cancelled: list[str] = []
        seen: set[str] = set()
        for parent_run_id in parent_run_ids:
            for subagent_id in self.cancel_child_subagent_tasks(parent_run_id, reason=reason):
                if subagent_id in seen:
                    continue
                seen.add(subagent_id)
                cancelled.append(subagent_id)
        return cancelled

    def get_subagent(self, subagent_id: str) -> SubagentRunRecord | None:
        return self._subagents.get(subagent_id)

    def get_run(self, run_id: str) -> AgentRunRecord | None:
        return self._runs.get(str(run_id or "").strip())

    def _register_subagent_names(
        self,
        subagent_id: str,
        *,
        agent_type: str = "",
        objective: str = "",
        prompt_summary: str = "",
    ) -> None:
        """Map human-friendly labels to a subagent id (latest-wins).

        MiniCode subagents have no explicit spawn ``name`` like cc, so we make
        them addressable by the closest stable labels: agent_type, objective,
        and prompt summary. Latest spawn wins a shared label.
        """
        for label in (agent_type, objective, prompt_summary):
            key = str(label or "").strip().casefold()
            if key:
                self._subagent_name_registry[key] = subagent_id

    def resolve_subagent_name(self, name: str) -> str:
        """Return the subagent id for a name/label, or '' if unknown.

        Falls back to the latest-registered id for that label; a live subagent
        id passed as-is still resolves via ``get_subagent`` at the call site.
        """
        key = str(name or "").strip().casefold()
        return self._subagent_name_registry.get(key, "")

    def mark_subagent_background(self, subagent_id: str) -> SubagentRunRecord | None:
        """Promote a synchronously-started subagent to a background one.

        Used when a sync delegation exceeds the auto-background deadline and the
        parent hands it off: the record must now surface in background listings
        and enqueue a parent notification on completion.
        """
        record = self._subagents.get(subagent_id)
        if record is None:
            return None
        record.background = True
        self._swarm_store.upsert_subagent(record.to_dict())
        self.write_metric("subagent_auto_backgrounded", {"subagent_id": subagent_id})
        return record

    def store_subagent_result(
        self,
        subagent_id: str,
        *,
        status: AgentRunStatus,
        content: str = "",
        error: str = "",
        duration_ms: int = 0,
        iterations: int = 0,
        tool_call_count: int = 0,
        timed_out: bool = False,
        usage: dict[str, Any] | None = None,
    ) -> SubagentResultRecord:
        input_tokens = int((usage or {}).get("input_tokens") or 0)
        output_tokens = int((usage or {}).get("output_tokens") or 0)
        # Reasoning tokens are billed output; roll them into the total so the
        # coordinator sees true delegation cost.
        reasoning_tokens = int((usage or {}).get("reasoning_output_tokens") or 0)
        total_tokens = input_tokens + output_tokens + reasoning_tokens
        record = SubagentResultRecord(
            subagent_id=subagent_id,
            status=status,
            content=content,
            error=error,
            duration_ms=duration_ms,
            iterations=iterations,
            tool_call_count=tool_call_count,
            timed_out=timed_out,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )
        # Durability precedes observability: once a waiter sees completion, the
        # result must already survive a process restart.
        self._swarm_store.upsert_subagent_result(record.to_dict())
        self._subagent_results[subagent_id] = record
        self._cap_retained_subagent_results(subagent_id)
        notification = self._enqueue_parent_notification_for_result(subagent_id, record)
        completion_event = self._subagent_completion_events.get(subagent_id)
        if completion_event is not None:
            completion_event.set()
        metric_payload = {
            "subagent_id": subagent_id,
            "status": status,
            "duration_ms": duration_ms,
            "iterations": iterations,
            "tool_call_count": tool_call_count,
            "timed_out": timed_out,
            "total_tokens": total_tokens,
        }
        if notification is not None:
            metric_payload["notification_id"] = notification.notification_id
            metric_payload["notification_status"] = notification.status
        self.write_metric("subagent_result_stored", metric_payload)
        return record

    def _cap_retained_subagent_results(self, subagent_id: str) -> None:
        """Evict the oldest retained results for this subagent's parent past the
        per-parent cap, so long coordinator sessions don't grow memory without
        bound and the per-delegation evidence-claim rescan stays cheap.
        """
        parent_run_id = str(getattr(self._subagents.get(subagent_id), "parent_run_id", "") or "")
        if not parent_run_id:
            return
        sibling_ids = [
            sid
            for sid in self._subagent_results
            if str(getattr(self._subagents.get(sid), "parent_run_id", "") or "") == parent_run_id
        ]
        while len(sibling_ids) > MAX_RETAINED_SUBAGENT_RESULTS_PER_PARENT:
            oldest = sibling_ids.pop(0)
            self._subagent_results.pop(oldest, None)
            self._subagent_completion_events.pop(oldest, None)

    def get_subagent_snapshot(
        self,
        subagent_id: str,
        *,
        include_result: bool = True,
    ) -> dict[str, Any] | None:
        record = self._subagents.get(subagent_id)
        result = self._subagent_results.get(subagent_id)
        task = self._subagent_tasks.get(subagent_id)
        # Retained in-memory results are capped per parent (FIFO eviction), but
        # every result is durably persisted first. On a memory miss, fall back to
        # the swarm store so an evicted-but-persisted result stays collectable
        # instead of surfacing as "No retained result".
        result_dict: dict[str, Any] | None = None
        if result is not None:
            result_dict = result.to_dict()
        else:
            stored = self._swarm_store.get_subagent_result(subagent_id)
            if isinstance(stored, dict):
                result_dict = stored
        if record is None and result_dict is None and task is None:
            return None
        payload: dict[str, Any] = record.to_dict() if record is not None else {"subagent_id": subagent_id}
        if task is not None:
            payload["background_task"] = "done" if task.done() else "running"
        cancel_event = self._subagent_cancel_events.get(subagent_id)
        if cancel_event is not None and cancel_event.is_set():
            payload["cancel_requested"] = True
        if result_dict is not None:
            payload["result_available"] = True
            if not payload.get("status") and result_dict.get("status"):
                payload["status"] = result_dict.get("status")
            if include_result:
                payload["result"] = result_dict
        else:
            payload["result_available"] = False
        return payload

    def list_subagent_results(self, parent_run_id: str) -> list[dict[str, Any]]:
        """Return retained results belonging to one coordinator run."""
        parent = str(parent_run_id or "").strip()
        if not parent:
            return []
        return [
            result.to_dict()
            for subagent_id, result in self._subagent_results.items()
            if str(getattr(self._subagents.get(subagent_id), "parent_run_id", "") or "") == parent
        ]

    def forget_subagent_result(self, subagent_id: str) -> bool:
        removed_memory = self._subagent_results.pop(subagent_id, None) is not None
        self._subagent_completion_events.pop(subagent_id, None)
        removed_store = self._swarm_store.delete_subagent_result(subagent_id)
        removed = removed_memory or removed_store
        self.write_metric("subagent_result_forgotten", {"subagent_id": subagent_id, "removed": removed})
        return removed

    def execution_journal(self, agent_id: str) -> ExecutionJournal:
        return ExecutionJournal(agent_id, base_dir=self._journal_root)

    def load_agent_transcript(self, agent_id: str) -> dict[str, Any]:
        return load_agent_transcript(agent_id, base_dir=self._journal_root)

    def parent_outbox(
        self,
        *,
        parent_run_id: str = "",
        conversation_id: str = "",
    ) -> ParentNotificationOutbox:
        return load_parent_outbox(
            parent_run_id=parent_run_id,
            conversation_id=conversation_id,
            base_dir=self._outbox_root,
        )

    def _enqueue_parent_notification_for_result(
        self,
        subagent_id: str,
        result: SubagentResultRecord,
    ) -> ParentNotification | None:
        record = self._subagents.get(subagent_id)
        parent_run_id = str(getattr(record, "parent_run_id", "") or "").strip()
        conversation_id = ""
        if parent_run_id:
            parent_run = self._runs.get(parent_run_id)
            if parent_run is not None:
                conversation_id = str(getattr(parent_run, "conversation_id", "") or "").strip()
        if not parent_run_id and not conversation_id:
            return None
        # Synchronous TaskTool results already return to the parent as tool_result.
        # Only background / detach completions need outbox -> next-turn injection
        # (Claude Code enqueueAgentNotification is for async agents).
        is_background = bool(getattr(record, "background", False)) if record else False
        is_detach = bool(getattr(record, "detach_from_parent", False)) if record else False
        if not (is_background or is_detach):
            return None
        payload = {
            "status": result.status,
            "content": result.content,
            "error": result.error,
            "duration_ms": result.duration_ms,
            "iterations": result.iterations,
            "tool_call_count": result.tool_call_count,
            "timed_out": result.timed_out,
            "completed_at": result.completed_at,
            "required_for_final": bool(getattr(record, "required_for_final", True)) if record else True,
            "detach_from_parent": bool(getattr(record, "detach_from_parent", False)) if record else False,
            "cancel_with_parent": bool(getattr(record, "cancel_with_parent", True)) if record else True,
            "agent_type": str(getattr(record, "agent_type", "") or "") if record else "",
            "prompt_summary": str(getattr(record, "prompt_summary", "") or "") if record else "",
        }
        notification = enqueue_parent_notification(
            parent_run_id=parent_run_id,
            conversation_id=conversation_id,
            subagent_id=subagent_id,
            payload=payload,
            kind="subagent_completed",
            idempotency_key=f"subagent_completed:{subagent_id}:{result.status}:{result.completed_at}",
            base_dir=self._outbox_root,
        )
        self.write_metric(
            "parent_notification_enqueued",
            {
                "notification_id": notification.notification_id,
                "parent_run_id": parent_run_id,
                "conversation_id": conversation_id,
                "subagent_id": subagent_id,
                "status": notification.status,
            },
        )
        return notification

    def list_parent_notifications(
        self,
        *,
        parent_run_id: str = "",
        conversation_id: str = "",
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        outbox = self.parent_outbox(
            parent_run_id=parent_run_id,
            conversation_id=conversation_id,
        )
        return [item.to_dict() for item in outbox.list_notifications(status=status)]

    def ack_parent_notification(
        self,
        notification_id: str,
        *,
        parent_run_id: str = "",
        conversation_id: str = "",
    ) -> dict[str, Any] | None:
        outbox = self.parent_outbox(
            parent_run_id=parent_run_id,
            conversation_id=conversation_id,
        )
        item = outbox.ack(notification_id)
        if item is None:
            return None
        self.write_metric(
            "parent_notification_acked",
            {
                "notification_id": item.notification_id,
                "parent_run_id": item.parent_run_id,
                "subagent_id": item.subagent_id,
            },
        )
        return item.to_dict()

    def replay_parent_notifications(
        self,
        *,
        parent_run_id: str = "",
        conversation_id: str = "",
    ) -> list[dict[str, Any]]:
        outbox = self.parent_outbox(
            parent_run_id=parent_run_id,
            conversation_id=conversation_id,
        )
        replayed: list[dict[str, Any]] = []
        for item in outbox.replayable():
            delivered = outbox.mark_delivered(item.notification_id) or item
            replayed.append(delivered.to_dict())
        if replayed:
            self.write_metric(
                "parent_notifications_replayed",
                {
                    "parent_run_id": parent_run_id,
                    "conversation_id": conversation_id,
                    "count": len(replayed),
                },
            )
        return replayed

    def send_swarm_message(
        self,
        *,
        sender_id: str,
        recipient_id: str,
        content: str,
        conversation_id: str = "",
        team_name: str = "",
        task_id: str = "",
        message_id: str = "",
    ) -> SwarmMessageRecord:
        record = _swarm_message_from_dict(
            self._swarm_store.append_message({
                "sender_id": sender_id,
                "recipient_id": recipient_id,
                "content": content,
                "conversation_id": conversation_id,
                "team_name": team_name,
                "task_id": task_id,
                "message_id": message_id,
            })
        )
        self._swarm_messages[record.message_id] = record
        self.write_metric("swarm_message_sent", record.to_dict())
        return record

    def list_swarm_messages(
        self,
        *,
        participant_id: str = "",
        conversation_id: str = "",
        since_seq: int = 0,
        limit: int = 20,
    ) -> list[SwarmMessageRecord]:
        records = [
            _swarm_message_from_dict(item)
            for item in self._swarm_store.list_messages(
                participant_id=participant_id,
                conversation_id=conversation_id,
                since_seq=since_seq,
                limit=limit,
            )
        ]
        for record in records:
            self._swarm_messages[record.message_id] = record
        return records

    def create_swarm_task(
        self,
        *,
        title: str,
        description: str = "",
        assignee: str = "",
        status: SwarmTaskStatus = "pending",
        priority: str = "normal",
        team_name: str = "",
        created_by: str = "",
        conversation_id: str = "",
        blocks: list[str] | None = None,
        blocked_by: list[str] | None = None,
        workflow_id: str = "",
        workflow_name: str = "",
        workflow_mode: str = "",
        node_id: str = "",
        agent_type: str = "general-purpose",
        role: str = "",
        objective: str = "",
        required_for_final: bool = True,
        read_only: bool = False,
        write_scope: list[str] | None = None,
    ) -> SwarmTaskRecord:
        task = _swarm_task_from_dict(
            self._swarm_store.create_task({
                "title": title,
                "description": description,
                "assignee": assignee,
                "conversation_id": conversation_id,
                "status": status,
                "priority": priority,
                "team_name": team_name,
                "created_by": created_by,
                "blocks": blocks or [],
                "blocked_by": blocked_by or [],
                "workflow_id": workflow_id,
                "workflow_name": workflow_name,
                "workflow_mode": workflow_mode,
                "node_id": node_id,
                "agent_type": agent_type,
                "role": role,
                "objective": objective,
                "required_for_final": required_for_final,
                "read_only": read_only,
                "write_scope": write_scope or [],
            })
        )
        self._swarm_tasks[task.task_id] = task
        self.write_metric("swarm_task_created", task.to_dict())
        return task

    def get_swarm_task(self, task_id: str) -> SwarmTaskRecord | None:
        payload = self._swarm_store.get_task(task_id)
        if payload is None:
            return self._swarm_tasks.get(task_id)
        task = _swarm_task_from_dict(payload)
        self._swarm_tasks[task.task_id] = task
        return task

    def list_swarm_tasks(
        self,
        *,
        assignee: str = "",
        status: str = "",
        team_name: str = "",
        conversation_id: str = "",
        since_seq: int = 0,
        limit: int = 50,
    ) -> list[SwarmTaskRecord]:
        records = [
            _swarm_task_from_dict(item)
            for item in self._swarm_store.list_tasks(
                assignee=assignee,
                status=status,
                team_name=team_name,
                conversation_id=conversation_id,
                since_seq=since_seq,
                limit=limit,
            )
        ]
        for record in records:
            self._swarm_tasks[record.task_id] = record
        return records

    def update_swarm_task(self, task_id: str, patch: dict[str, Any]) -> SwarmTaskRecord | None:
        payload = self._swarm_store.update_task(task_id, patch)
        if payload is None:
            return None
        task = _swarm_task_from_dict(payload)
        self._swarm_tasks[task.task_id] = task
        self.write_metric("swarm_task_updated", task.to_dict())
        return task

    def append_swarm_task_output(
        self,
        task_id: str,
        *,
        author_id: str,
        content: str,
    ) -> SwarmTaskRecord | None:
        payload = self._swarm_store.append_output(task_id, {"author_id": author_id, "content": content})
        if payload is None:
            return None
        task = _swarm_task_from_dict(payload)
        self._swarm_tasks[task.task_id] = task
        self.write_metric("swarm_task_output", task.to_dict())
        return task

    def register_workflow_launcher(self, workflow_id: str, launcher: WorkflowLauncher) -> None:
        workflow_id = str(workflow_id or "").strip()
        if not self._workflow_coordinator.register_launcher(workflow_id, launcher):
            return
        self.write_metric("workflow_launcher_registered", {"workflow_id": workflow_id})

    def list_workflow_tasks(self, workflow_id: str, conversation_id: str) -> list[SwarmTaskRecord]:
        """WorkflowCoordinator persistence port."""
        return self.list_swarm_tasks(
            team_name=workflow_id,
            conversation_id=conversation_id,
            limit=100,
        )

    def update_workflow_task(self, task_id: str, patch: dict[str, Any]) -> SwarmTaskRecord | None:
        """WorkflowCoordinator persistence port."""
        return self.update_swarm_task(task_id, patch)

    def write_workflow_metric(self, event: str, payload: dict[str, Any]) -> None:
        """WorkflowCoordinator metrics port."""
        self.write_metric(event, payload)

    async def resume_pending_workflow(
        self,
        workflow_id: str,
        *,
        conversation_id: str = "",
    ) -> dict[str, Any]:
        """Launch persisted pending workflow tasks whose dependencies are satisfied."""
        return await self._workflow_coordinator.resume_pending_workflow(
            self,
            workflow_id,
            conversation_id=conversation_id,
        )

    async def advance_workflow(
        self,
        workflow_id: str,
        *,
        conversation_id: str = "",
    ) -> dict[str, Any]:
        """Unblock and optionally launch workflow tasks whose blockers completed."""
        return await self._workflow_coordinator.advance_workflow(
            self,
            workflow_id,
            conversation_id=conversation_id,
        )

    async def cancel_workflow_dependents(
        self,
        workflow_id: str,
        *,
        conversation_id: str = "",
    ) -> dict[str, Any]:
        """Cancel blocked/pending nodes that depend on cancelled workflow tasks."""
        return await self._workflow_coordinator.cancel_workflow_dependents(
            self,
            workflow_id,
            conversation_id=conversation_id,
        )

    def workflow_completion_snapshot(
        self,
        workflow_id: str,
        *,
        conversation_id: str = "",
    ) -> dict[str, Any]:
        return self._workflow_coordinator.workflow_completion_snapshot(
            self,
            workflow_id,
            conversation_id=conversation_id,
        )

    def complete_workflow_if_ready(
        self,
        workflow_id: str,
        *,
        conversation_id: str = "",
    ) -> dict[str, Any] | None:
        return self._workflow_coordinator.complete_workflow_if_ready(
            self,
            workflow_id,
            conversation_id=conversation_id,
        )

    def create_swarm_team(
        self,
        *,
        team_name: str,
        description: str = "",
        members: list[dict[str, Any]] | None = None,
        conversation_id: str = "",
        created_by: str = "",
    ) -> SwarmTeamRecord:
        team = _swarm_team_from_dict(
            self._swarm_store.create_team({
                "team_name": team_name,
                "description": description,
                "members": members or [],
                "conversation_id": conversation_id,
                "created_by": created_by,
            })
        )
        self._swarm_teams[team.team_name] = team
        self.write_metric("swarm_team_created", team.to_dict())
        return team

    def list_swarm_teams(
        self,
        *,
        conversation_id: str = "",
        team_name: str = "",
        since_seq: int = 0,
        limit: int = 50,
    ) -> list[SwarmTeamRecord]:
        records = [
            _swarm_team_from_dict(item)
            for item in self._swarm_store.list_teams(
                conversation_id=conversation_id,
                team_name=team_name,
                since_seq=since_seq,
                limit=limit,
            )
        ]
        for record in records:
            self._swarm_teams[record.team_name] = record
        return records

    def delete_swarm_team(
        self,
        *,
        conversation_id: str = "",
        team_name: str,
    ) -> SwarmTeamRecord | None:
        payload = self._swarm_store.delete_team(conversation_id=conversation_id, team_name=team_name)
        if payload is None:
            return None
        team = _swarm_team_from_dict(payload)
        self._swarm_teams.pop(team.team_name, None)
        self.write_metric("swarm_team_deleted", team.to_dict())
        return team

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
                {
                    **record.to_dict(),
                    **({"background_task": "running"} if record.subagent_id in self._subagent_tasks else {}),
                    **({"result_available": True} if record.subagent_id in self._subagent_results else {}),
                    **(
                        {"cancel_requested": True}
                        if (
                            record.subagent_id in self._subagent_cancel_events
                            and self._subagent_cancel_events[record.subagent_id].is_set()
                        )
                        else {}
                    ),
                }
                for record in self._subagents.values()
                if not conversation_id or record.parent_run_id in parent_ids
            ]
            payload["swarm_messages"] = [
                record.to_dict()
                for record in self.list_swarm_messages(conversation_id=conversation_id, limit=20)
            ]
            payload["swarm_tasks"] = [
                record.to_dict()
                for record in self.list_swarm_tasks(conversation_id=conversation_id, limit=50)
            ]
            payload["swarm_teams"] = [
                record.to_dict()
                for record in self.list_swarm_teams(conversation_id=conversation_id, limit=50)
            ]
        return payload


_DEFAULT_RUNTIME = AgentRuntime()


def default_runtime() -> AgentRuntime:
    return _DEFAULT_RUNTIME
