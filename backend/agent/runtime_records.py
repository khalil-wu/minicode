"""Durable run/subagent/swarm records and their JSON serialization.

Extracted from ``backend/agent/runtime.py`` so the record layer (pure data
classes plus deterministic ``_from_dict`` deserializers) is independently
testable and the runtime class stays focused on orchestration, leases and
process-local coordination.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal
from uuid import uuid4

from backend.config import DATA_ROOT, TokenBudget

SWARM_DIR = DATA_ROOT / "swarm"

from backend.agent.public_projection import (
    project_public_agent_run,
    project_public_subagent_result,
    project_public_subagent_run,
    project_public_swarm_message,
    project_public_swarm_task,
    project_public_swarm_task_output,
    project_public_swarm_team,
    project_public_swarm_team_member,
)

# ---------------------------------------------------------------------------
# Agent run taxonomy
# ---------------------------------------------------------------------------

AgentRunStatus = Literal["running", "completed", "partial", "failed", "cancelled", "interrupted"]
AgentRunPhase = Literal["plan", "execute", "recover", "final"]
SwarmTaskStatus = Literal["pending", "in_progress", "blocked", "completed", "cancelled"]



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
    terminal_reason: str = ""
    error: str = ""
    runtime_instance_id: str = ""
    runtime_process_id: int = 0
    runtime_process_start_identity: str = ""
    runtime_owner_token: str = ""
    agent_path: str = ""
    mailbox_epoch: int = 0
    cleanup_pending: bool = False
    cleanup_reason: str = ""
    cleanup_requested_at: int | None = None
    cleanup_completed_at: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def public_dict(self) -> dict[str, Any]:
        return project_public_agent_run(asdict(self))

    def with_phase(self, phase: AgentRunPhase, *, summary: str = "") -> "AgentRunRecord":
        self.phase = phase
        if summary:
            self.summary = summary
        return self

    def complete(
        self,
        status: AgentRunStatus = "completed",
        *,
        summary: str = "",
        terminal_reason: str = "",
        error: str = "",
    ) -> "AgentRunRecord":
        self.status = status
        self.phase = "final"
        self.completed_at = epoch_ms()
        if summary:
            self.summary = summary
        if terminal_reason:
            self.terminal_reason = terminal_reason
        if error:
            self.error = error
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
    task_id: str = ""
    session_id: str = ""
    objective: str = ""
    depends_on: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    cancel_with_parent: bool = True
    detach_from_parent: bool = False
    read_only: bool = False
    write_scope: list[str] = field(default_factory=list)
    resume_config: dict[str, Any] = field(default_factory=dict)
    current_activity: str = ""
    status: AgentRunStatus = "running"
    tool_count: int = 0
    result_summary: str = ""
    checkpoint_id: str = ""
    started_at: int = field(default_factory=epoch_ms)
    completed_at: int | None = None
    cleanup_pending: bool = False
    cleanup_reason: str = ""
    cleanup_requested_at: int | None = None
    cleanup_completed_at: int | None = None
    cleanup_resources: list[dict[str, Any]] = field(default_factory=list)
    runtime_instance_id: str = ""
    runtime_process_id: int = 0
    runtime_process_start_identity: str = ""
    runtime_owner_token: str = ""
    agent_path: str = ""
    mailbox_epoch: int = 0
    # Named teammate identity/lifecycle fields. Ordinary bounded subagents leave
    # these empty/false; named teammates persist them with the same
    # durable incarnation record used for mailbox fencing.
    teammate_name: str = ""
    team_name: str = ""
    permission_mode: str = "confirm"
    plan_mode_required: bool = False
    awaiting_plan_approval: bool = False
    active_plan_request_id: str = ""
    is_idle: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def public_dict(self) -> dict[str, Any]:
        return project_public_subagent_run(asdict(self))

    def complete(self, status: AgentRunStatus = "completed", *, summary: str = "", tool_count: int = 0) -> "SubagentRunRecord":
        self.status = status
        self.completed_at = epoch_ms()
        if summary:
            self.result_summary = summary
        self.tool_count = tool_count
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
    # Preserve the provider/runtime terminal reason separately from the
    # human-facing status so the parent can distinguish partial outcomes.
    terminal_reason: str = ""
    # Token usage rolled up from the child's terminal ``done`` event so the
    # coordinator can see delegation cost, not just wall-clock/tool counts.
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    # Preserve the provider-neutral usage envelope for dialect result codecs.
    # The scalar totals above remain the durable/query-friendly compatibility
    # fields; this mapping carries cache, server-tool, service-tier and other
    # public usage details without forcing every provider into one schema.
    usage: dict[str, Any] = field(default_factory=dict)
    artifact_id: str = ""
    agent_path: str = ""
    mailbox_epoch: int = 0
    runtime_owner_token: str = ""
    completed_at: int = field(default_factory=epoch_ms)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def public_dict(self, *, content_override: Any | None = None) -> dict[str, Any]:
        return project_public_subagent_result(
            asdict(self),
            content_override=content_override,
        )


@dataclass
class SwarmMessageRecord:
    message_id: str
    sender_id: str
    recipient_id: str
    content: str
    conversation_id: str = ""
    team_name: str = ""
    task_id: str = ""
    sender_mailbox_epoch: int = 0
    recipient_mailbox_epoch: int = 0
    recipient_mailbox_epochs: dict[str, int] = field(default_factory=dict)
    created_at: int = field(default_factory=epoch_ms)
    seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def public_dict(self) -> dict[str, Any]:
        return project_public_swarm_message(asdict(self))


@dataclass(frozen=True)
class MailboxMessageClaim:
    message: SwarmMessageRecord
    participant_id: str
    mailbox_epoch: int
    claim_token: str
    lease_expires_at: int

    def claim_ref(self) -> dict[str, Any]:
        return {
            "message_id": self.message.message_id,
            "participant_id": self.participant_id,
            "mailbox_epoch": self.mailbox_epoch,
            "claim_token": self.claim_token,
        }


@dataclass
class SwarmTaskOutputRecord:
    output_id: str
    author_id: str
    content: str
    created_at: int = field(default_factory=epoch_ms)
    seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def public_dict(self) -> dict[str, Any]:
        return project_public_swarm_task_output(asdict(self))


@dataclass
class SwarmTaskRecord:
    task_id: str
    title: str
    description: str = ""
    assignee: str = ""
    conversation_id: str = ""
    agent_type: str = "general-purpose"
    role: str = ""
    objective: str = ""
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

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["outputs"] = [output.public_dict() for output in self.outputs]
        return project_public_swarm_task(data)

    def update(self, patch: dict[str, Any]) -> None:
        for key in (
            "title",
            "description",
            "assignee",
            "priority",
            "team_name",
            "agent_type",
            "role",
            "objective",
        ):
            if key in patch:
                setattr(self, key, str(patch[key] or "").strip())
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

    def public_dict(self) -> dict[str, Any]:
        return project_public_swarm_team_member(asdict(self))


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

    @property
    def lead_agent_id(self) -> str:
        return f"team-lead@{self.team_name}"

    @property
    def team_file_path(self) -> str:
        # MiniCode's durable team store is the swarm SQLite database, so report
        # that real path
        # instead of an invented swarm:// URI.
        return str(SWARM_DIR / "swarm.sqlite3")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["members"] = [member.to_dict() for member in self.members]
        return data

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["members"] = [member.public_dict() for member in self.members]
        return project_public_swarm_team(data)


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
        sender_mailbox_epoch=max(0, int(data.get("sender_mailbox_epoch") or 0)),
        recipient_mailbox_epoch=max(0, int(data.get("recipient_mailbox_epoch") or 0)),
        recipient_mailbox_epochs={
            str(key): max(0, int(value or 0))
            for key, value in (data.get("recipient_mailbox_epochs") or {}).items()
            if str(key).strip()
        } if isinstance(data.get("recipient_mailbox_epochs"), dict) else {},
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
        agent_type=str(data.get("agent_type") or "general-purpose") or "general-purpose",
        role=str(data.get("role") or ""),
        objective=str(data.get("objective") or ""),
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
        task_id=str(data.get("task_id") or ""),
        session_id=str(data.get("session_id") or ""),
        objective=str(data.get("objective") or ""),
        depends_on=_string_list(data.get("depends_on")),
        blocked_by=_string_list(data.get("blocked_by")),
        cancel_with_parent=bool(
            data.get(
                "cancel_with_parent",
                not bool(data.get("detach_from_parent", False)),
            )
        ),
        detach_from_parent=bool(data.get("detach_from_parent", False)),
        read_only=bool(data.get("read_only", False)),
        write_scope=_string_list(data.get("write_scope")),
        resume_config=(
            dict(data.get("resume_config") or {})
            if isinstance(data.get("resume_config"), dict)
            else {}
        ),
        # Durable round-trips must retain teammate and permission lifecycle fields.
        teammate_name=str(data.get("teammate_name") or ""),
        team_name=str(data.get("team_name") or ""),
        permission_mode=str(data.get("permission_mode") or ""),
        plan_mode_required=bool(data.get("plan_mode_required", False)),
        awaiting_plan_approval=bool(data.get("awaiting_plan_approval", False)),
        active_plan_request_id=str(data.get("active_plan_request_id") or ""),
        current_activity=str(data.get("current_activity") or ""),
        status=str(data.get("status") or "running"),  # type: ignore[arg-type]
        tool_count=int(data.get("tool_count") or 0),
        result_summary=str(data.get("result_summary") or ""),
        checkpoint_id=str(data.get("checkpoint_id") or ""),
        started_at=int(data.get("started_at") or epoch_ms()),
        completed_at=data.get("completed_at") if isinstance(data.get("completed_at"), int) else None,
        cleanup_pending=bool(data.get("cleanup_pending", False)),
        cleanup_reason=str(data.get("cleanup_reason") or ""),
        cleanup_requested_at=(
            data.get("cleanup_requested_at")
            if isinstance(data.get("cleanup_requested_at"), int)
            else None
        ),
        cleanup_completed_at=(
            data.get("cleanup_completed_at")
            if isinstance(data.get("cleanup_completed_at"), int)
            else None
        ),
        cleanup_resources=[
            dict(item)
            for item in data.get("cleanup_resources", [])
            if isinstance(item, dict)
        ],
        runtime_instance_id=str(data.get("runtime_instance_id") or ""),
        runtime_process_id=int(data.get("runtime_process_id") or 0),
        runtime_process_start_identity=str(data.get("runtime_process_start_identity") or ""),
        runtime_owner_token=str(data.get("runtime_owner_token") or ""),
        agent_path=str(data.get("agent_path") or ""),
        mailbox_epoch=int(data.get("mailbox_epoch") or 0),
    )


def _agent_run_from_dict(data: dict[str, Any]) -> AgentRunRecord:
    raw_phase = data.get("phase")
    phase = str(raw_phase or "plan")
    invalid_phase = False
    if phase == "verify":
        phase = "execute"
    elif phase not in {"plan", "execute", "recover", "final"}:
        invalid_phase = raw_phase is not None
        phase = "final" if invalid_phase else "plan"
    raw_status = data.get("status")
    status = str(raw_status or "running")
    invalid_status = raw_status is not None and status not in {
        "running", "completed", "partial", "failed", "cancelled", "interrupted"
    }
    if invalid_status or invalid_phase:
        # A present-but-invalid durable status is corruption, not evidence of
        # a live task. Fail closed so restore cannot resurrect malformed data.
        status = "interrupted"
        phase = "final"
    completed_at = data.get("completed_at") if isinstance(data.get("completed_at"), int) else None
    if invalid_status and completed_at is None:
        completed_at = epoch_ms()
    terminal_reason = str(data.get("terminal_reason") or "")
    error = str(data.get("error") or "")
    if invalid_status or invalid_phase:
        terminal_reason = terminal_reason or (
            "invalid_persisted_status" if invalid_status else "invalid_persisted_phase"
        )
        error = error or (
            f"invalid persisted run status: {raw_status!r}"
            if invalid_status
            else f"invalid persisted run phase: {raw_phase!r}"
        )
    return AgentRunRecord(
        run_id=str(data.get("run_id") or ""),
        conversation_id=str(data.get("conversation_id") or ""),
        parent_run_id=str(data.get("parent_run_id") or ""),
        role=str(data.get("role") or "main"),
        phase=phase,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        budget=dict(data.get("budget") or {}),
        started_at=int(data.get("started_at") or epoch_ms()),
        completed_at=completed_at,
        task_id=str(data.get("task_id") or ""),
        session_id=str(data.get("session_id") or ""),
        summary=str(data.get("summary") or ""),
        terminal_reason=terminal_reason,
        error=error,
        runtime_instance_id=str(data.get("runtime_instance_id") or ""),
        runtime_process_id=int(data.get("runtime_process_id") or 0),
        runtime_process_start_identity=str(data.get("runtime_process_start_identity") or ""),
        runtime_owner_token=str(data.get("runtime_owner_token") or ""),
        agent_path=str(data.get("agent_path") or ""),
        mailbox_epoch=int(data.get("mailbox_epoch") or 0),
        cleanup_pending=bool(data.get("cleanup_pending", False)),
        cleanup_reason=str(data.get("cleanup_reason") or ""),
        cleanup_requested_at=(
            data.get("cleanup_requested_at")
            if isinstance(data.get("cleanup_requested_at"), int)
            else None
        ),
        cleanup_completed_at=(
            data.get("cleanup_completed_at")
            if isinstance(data.get("cleanup_completed_at"), int)
            else None
        ),
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
        terminal_reason=str(data.get("terminal_reason") or ""),
        input_tokens=int(data.get("input_tokens") or 0),
        output_tokens=int(data.get("output_tokens") or 0),
        total_tokens=int(data.get("total_tokens") or 0),
        usage=(
            dict(data.get("usage") or {})
            if isinstance(data.get("usage"), dict)
            else {}
        ),
        artifact_id=str(data.get("artifact_id") or ""),
        agent_path=str(data.get("agent_path") or ""),
        mailbox_epoch=max(0, int(data.get("mailbox_epoch") or 0)),
        runtime_owner_token=str(data.get("runtime_owner_token") or ""),
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


