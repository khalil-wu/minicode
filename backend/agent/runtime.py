"""Runtime records and metrics for MiniCode's agent control plane.

This module is deliberately small: the existing ReAct loop remains the
execution kernel, while AgentRuntime gives WebSocket/UI layers one stable
shape for runs, phases, subagents, checkpoints, and local observability.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field, replace
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
from backend.agent.agent_identity import AgentPath, MailboxEpoch
from backend.agent.agent_registry import AgentRegistry
from backend.agent.swarm_store import FileSwarmStore

AgentRunStatus = Literal["running", "completed", "partial", "failed", "cancelled", "interrupted"]
AgentRunPhase = Literal["plan", "execute", "recover", "final"]
SwarmTaskStatus = Literal["pending", "in_progress", "blocked", "completed", "cancelled"]

# ---------------------------------------------------------------------------
# Explicit four-type Agent taxonomy (plan §11.2)
# ---------------------------------------------------------------------------

AgentRole = Literal["primary", "subagent", "side_query", "background"]

AGENT_ROLES: frozenset[str] = frozenset({"primary", "subagent", "side_query", "background"})

# Pi's reference subagent extension executes four independent workers in
# parallel. Lifetime is governed by the persisted run and cancellation records;
# there is no cumulative per-session delegation quota.
MAX_CONCURRENT_SUBAGENTS = 4

# Write-scope strategy: subagents may be confined to a git worktree so that
# their file mutations do not leak into the primary workspace until merged.
WRITE_SCOPE_STRATEGIES: frozenset[str] = frozenset({"none", "workspace", "worktree", "readonly"})

METRICS_DIR = DATA_ROOT / "metrics"
METRICS_FILE = METRICS_DIR / "agent_metrics.jsonl"
SWARM_DIR = DATA_ROOT / "swarm"
_RUNTIME_INSTANCE_ID = uuid4().hex
RUNTIME_LEASE_TTL_MS = max(
    10_000,
    int(os.environ.get("MINICODE_RUNTIME_LEASE_TTL_MS", "45000")),
)
RUNTIME_HEARTBEAT_INTERVAL_MS = max(
    1_000,
    min(
        RUNTIME_LEASE_TTL_MS // 3,
        int(os.environ.get("MINICODE_RUNTIME_HEARTBEAT_INTERVAL_MS", "10000")),
    ),
)


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


def _process_start_identity(process_id: int) -> str:
    """Return an OS process birth identity that changes when a PID is reused."""
    if process_id <= 0:
        return ""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class _FILETIME(ctypes.Structure):
            _fields_ = (("low", wintypes.DWORD), ("high", wintypes.DWORD))

        handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
            0x1000,
            False,
            process_id,
        )
        if not handle:
            return ""
        try:
            created = _FILETIME()
            exited = _FILETIME()
            kernel = _FILETIME()
            user = _FILETIME()
            ok = ctypes.windll.kernel32.GetProcessTimes(  # type: ignore[attr-defined]
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            )
            if not ok:
                return ""
            ticks = (int(created.high) << 32) | int(created.low)
            return f"windows-filetime:{ticks}"
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
    if sys.platform == "linux":
        try:
            stat = Path(f"/proc/{process_id}/stat").read_text(encoding="utf-8")
            # The command name is parenthesized and may contain spaces. Field
            # 22 (starttime) is index 19 after the closing parenthesis.
            fields = stat.rsplit(")", 1)[1].strip().split()
            return f"linux-starttime:{fields[19]}"
        except (OSError, IndexError):
            return ""
    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(process_id)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    started = completed.stdout.strip()
    return f"ps-lstart:{started}" if completed.returncode == 0 and started else ""


def _process_matches_identity(process_id: int, expected_identity: str) -> bool:
    expected = str(expected_identity or "").strip()
    return bool(expected and _process_start_identity(process_id) == expected)


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
    runtime_instance_id: str = ""
    runtime_process_id: int = 0
    runtime_process_start_identity: str = ""
    runtime_owner_token: str = ""
    agent_path: str = ""

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
    objective: str = ""
    depends_on: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
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
    runtime_instance_id: str = ""
    runtime_process_id: int = 0
    runtime_process_start_identity: str = ""
    runtime_owner_token: str = ""
    agent_path: str = ""
    mailbox_epoch: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

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
    artifact_id: str = ""
    agent_path: str = ""
    mailbox_epoch: int = 0
    runtime_owner_token: str = ""
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
    sender_mailbox_epoch: int = 0
    recipient_mailbox_epoch: int = 0
    recipient_mailbox_epochs: dict[str, int] = field(default_factory=dict)
    created_at: int = field(default_factory=epoch_ms)
    seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
        current_activity=str(data.get("current_activity") or ""),
        status=str(data.get("status") or "running"),  # type: ignore[arg-type]
        tool_count=int(data.get("tool_count") or 0),
        result_summary=str(data.get("result_summary") or ""),
        checkpoint_id=str(data.get("checkpoint_id") or ""),
        started_at=int(data.get("started_at") or epoch_ms()),
        completed_at=data.get("completed_at") if isinstance(data.get("completed_at"), int) else None,
        runtime_instance_id=str(data.get("runtime_instance_id") or ""),
        runtime_process_id=int(data.get("runtime_process_id") or 0),
        runtime_process_start_identity=str(data.get("runtime_process_start_identity") or ""),
        runtime_owner_token=str(data.get("runtime_owner_token") or ""),
        agent_path=str(data.get("agent_path") or ""),
        mailbox_epoch=int(data.get("mailbox_epoch") or 0),
    )


def _agent_run_from_dict(data: dict[str, Any]) -> AgentRunRecord:
    phase = str(data.get("phase") or "plan")
    if phase == "verify":
        phase = "execute"
    elif phase not in {"plan", "execute", "recover", "final"}:
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
        runtime_instance_id=str(data.get("runtime_instance_id") or ""),
        runtime_process_id=int(data.get("runtime_process_id") or 0),
        runtime_process_start_identity=str(data.get("runtime_process_start_identity") or ""),
        runtime_owner_token=str(data.get("runtime_owner_token") or ""),
        agent_path=str(data.get("agent_path") or ""),
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


class AgentRuntime:
    """Tracks agent runs and appends local JSONL metrics."""

    def __init__(
        self,
        *,
        metrics_file: Path | None = None,
        swarm_store_dir: Path | None = None,
        runtime_instance_id: str | None = None,
        runtime_process_id: int | None = None,
        runtime_process_start_identity: str | None = None,
        runtime_owner_token: str | None = None,
        lease_ttl_ms: int = RUNTIME_LEASE_TTL_MS,
        enable_lease_heartbeat: bool | None = None,
    ) -> None:
        self._metrics_file = metrics_file or METRICS_FILE
        self._runtime_instance_id = runtime_instance_id or _RUNTIME_INSTANCE_ID
        self._runtime_process_id = runtime_process_id or os.getpid()
        self._runtime_process_start_identity = (
            str(runtime_process_start_identity).strip()
            if runtime_process_start_identity is not None
            else _process_start_identity(self._runtime_process_id)
        )
        self._lease_ttl_ms = max(1_000, int(lease_ttl_ms))
        self._lease_lost = False
        self._lease_stop_event: threading.Event | None = None
        self._lease_thread: threading.Thread | None = None
        store_dir = swarm_store_dir or ((metrics_file.parent / "swarm") if metrics_file is not None else SWARM_DIR)
        self._swarm_store = FileSwarmStore(store_dir)
        lease = self._swarm_store.claim_runtime_lease(
            runtime_instance_id=self._runtime_instance_id,
            requested_owner_token=runtime_owner_token or uuid4().hex,
            process_id=self._runtime_process_id,
            process_start_identity=self._runtime_process_start_identity,
            now_ms=epoch_ms(),
            ttl_ms=self._lease_ttl_ms,
        )
        if not bool(lease.get("acquired")):
            raise RuntimeError(
                "Agent runtime instance is already owned by another live process "
                f"(instance={self._runtime_instance_id}, pid={lease.get('process_id')})."
            )
        self._runtime_owner_token = str(lease["owner_token"])
        self._lease_expires_at = int(lease["expires_at"])
        # Keep sidechain journals / parent outboxes next to the swarm store so
        # isolated runtime fixtures and production share the same root layout.
        self._journal_root = store_dir.parent / "sidechains"
        self._outbox_root = store_dir.parent / "parent_notifications"
        self._runs: dict[str, AgentRunRecord] = {}
        self._subagents: dict[str, SubagentRunRecord] = {}
        self._registry = AgentRegistry()
        self._subagent_tasks: dict[str, asyncio.Task[Any]] = {}
        self._subagent_task_metadata: dict[str, dict[str, Any]] = {}
        self._subagent_slot_reservations: set[str] = set()
        self._subagent_capacity_waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()
        self._subagent_cancel_events: dict[str, asyncio.Event] = {}
        self._subagent_completion_events: dict[str, asyncio.Event] = {}
        self._subagent_parent_run_ids: dict[str, str] = {}
        # A task/session ownership fence is retained even when an embedder did
        # not provide a parent run id. Explicit user cancellation and session
        # shutdown must still be able to find and stop the child.
        self._subagent_owner_task_ids: dict[str, str] = {}
        self._subagent_session_ids: dict[str, str] = {}
        self._subagent_results: dict[str, SubagentResultRecord] = {}
        # name -> subagent_id registry for by-name addressing (SendMessage).
        # Latest-wins, mirroring cc's agentNameRegistry.
        self._subagent_name_registry: dict[str, str] = {}
        self._swarm_messages: dict[str, SwarmMessageRecord] = {}
        self._swarm_tasks: dict[str, SwarmTaskRecord] = {}
        self._swarm_teams: dict[str, SwarmTeamRecord] = {}
        now = epoch_ms()
        active_owner_tokens = {self._runtime_owner_token}
        for candidate in self._swarm_store.list_runtime_leases():
            owner_token = str(candidate.get("owner_token") or "")
            if not owner_token or int(candidate.get("expires_at") or 0) <= now:
                continue
            if _process_matches_identity(
                int(candidate.get("process_id") or 0),
                str(candidate.get("process_start_identity") or ""),
            ):
                active_owner_tokens.add(owner_token)
        recovered = self._swarm_store.recover_runtime_state(
            interrupted_at=now,
            summary="Interrupted because the previous MiniCode process ended before completion.",
            current_instance_id=self._runtime_instance_id,
            current_owner_token=self._runtime_owner_token,
            current_process_id=self._runtime_process_id,
            current_process_start_identity=self._runtime_process_start_identity,
            active_owner_tokens=active_owner_tokens,
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
        for record in self._runs.values():
            self._registry.register(record, kind="run")
            if record.status != "running":
                self._registry.seal(record.run_id, kind="run")
        for record in self._subagents.values():
            self._registry.register(record, kind="subagent")
            if record.status != "running":
                self._registry.seal(record.subagent_id, kind="subagent")
        self._subagent_results = {
            record.subagent_id: record
            for item in recovered["results"]
            if (record := _subagent_result_from_dict(item)).subagent_id
        }
        for record in self._subagents.values():
            if record.status != "interrupted":
                continue
            if record.subagent_id not in self._subagent_results:
                summary = str(record.result_summary or "").strip() or (
                    "Interrupted because the previous MiniCode process ended before completion."
                )
                self.store_subagent_result(
                    record.subagent_id,
                    status="interrupted",
                    content=summary,
                    error="runtime_interrupted",
                    tool_call_count=record.tool_count,
                    agent_path=record.agent_path,
                    mailbox_epoch=record.mailbox_epoch,
                )
        heartbeat_enabled = (
            metrics_file is None and swarm_store_dir is None
            if enable_lease_heartbeat is None
            else bool(enable_lease_heartbeat)
        )
        if heartbeat_enabled:
            self._start_lease_heartbeat()

    def _refresh_runtime_lease(self) -> bool:
        if self._lease_lost:
            return False
        now = epoch_ms()
        refreshed = self._swarm_store.heartbeat_runtime_lease(
            runtime_instance_id=self._runtime_instance_id,
            owner_token=self._runtime_owner_token,
            process_id=self._runtime_process_id,
            process_start_identity=self._runtime_process_start_identity,
            now_ms=now,
            ttl_ms=self._lease_ttl_ms,
        )
        if refreshed:
            self._lease_expires_at = now + self._lease_ttl_ms
        else:
            self._lease_lost = True
        return refreshed

    def _start_lease_heartbeat(self) -> None:
        if self._lease_thread is not None:
            return
        stop_event = threading.Event()
        self._lease_stop_event = stop_event
        interval = min(
            RUNTIME_HEARTBEAT_INTERVAL_MS,
            max(1_000, self._lease_ttl_ms // 3),
        ) / 1000.0

        def _heartbeat_loop() -> None:
            while not stop_event.wait(interval):
                try:
                    if not self._refresh_runtime_lease():
                        return
                except Exception:
                    # A transient SQLite error must not terminate the loop. If
                    # the lease truly expires, a later owner claim changes the
                    # token and the next successful heartbeat is fenced out.
                    continue

        self._lease_thread = threading.Thread(
            target=_heartbeat_loop,
            name="minicode-agent-lease-heartbeat",
            daemon=True,
        )
        self._lease_thread.start()

    def close(self, *, release_lease: bool = False) -> bool:
        """Stop background lease maintenance and optionally relinquish ownership.

        ``release_lease`` is explicit because more than one observer runtime may
        share the same process-owned token. Normal application shutdown owns the
        process lifecycle and releases it; short-lived observers should only
        stop their own heartbeat thread.
        """

        stop_event = self._lease_stop_event
        thread = self._lease_thread
        if stop_event is not None:
            stop_event.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._lease_stop_event = None
        self._lease_thread = None
        if not release_lease or self._lease_lost:
            return False
        released = self._swarm_store.release_runtime_lease(
            runtime_instance_id=self._runtime_instance_id,
            owner_token=self._runtime_owner_token,
        )
        if released:
            self._lease_lost = True
        return released

    def _owns_record(self, record: AgentRunRecord | SubagentRunRecord) -> bool:
        return bool(
            not self._lease_lost
            and record.runtime_owner_token == self._runtime_owner_token
        )

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
        if not self._refresh_runtime_lease():
            raise RuntimeError("Agent runtime lease was lost; refusing to start a new run.")
        resolved_run_id = run_id or new_run_id()
        parent = self._runs.get(str(parent_run_id or "").strip())
        parent_path = AgentPath.parse(parent.agent_path) if parent and parent.agent_path else None
        record = AgentRunRecord(
            run_id=resolved_run_id,
            conversation_id=conversation_id,
            parent_run_id=parent_run_id,
            role=role,
            task_id=task_id,
            session_id=session_id,
            budget=budget_snapshot(budget),
            runtime_instance_id=self._runtime_instance_id,
            runtime_process_id=self._runtime_process_id,
            runtime_process_start_identity=self._runtime_process_start_identity,
            runtime_owner_token=self._runtime_owner_token,
            agent_path=(
                parent_path.child(task_id or resolved_run_id).value
                if parent_path and role != "main"
                else AgentPath.main(resolved_run_id).value
            ),
        )
        persisted = self._swarm_store.upsert_agent_run(
            record.to_dict(),
            expected_owner_token=self._runtime_owner_token,
            allow_takeover_terminal=True,
        )
        if persisted is None:
            raise RuntimeError(f"Agent run {record.run_id} is owned by another runtime.")
        self._runs[record.run_id] = record
        self._registry.register(record, kind="run")
        self.write_metric("run_started", record.to_dict())
        return record

    def update_phase(self, run_id: str, phase: AgentRunPhase, *, summary: str = "") -> AgentRunRecord | None:
        record = self._runs.get(run_id)
        if record is None:
            return None
        if not self._registry.accepts_update(
            agent_id=run_id,
            kind="run",
            agent_path=record.agent_path,
        ):
            return None
        if not self._owns_record(record) or not self._refresh_runtime_lease():
            return None
        candidate = replace(record).with_phase(phase, summary=summary)
        if self._swarm_store.upsert_agent_run(
            candidate.to_dict(),
            expected_owner_token=self._runtime_owner_token,
        ) is None:
            self._lease_lost = True
            return None
        self._runs[run_id] = candidate
        self.write_metric("phase_updated", candidate.to_dict())
        return candidate

    def complete_run(self, run_id: str, status: AgentRunStatus = "completed", *, summary: str = "", error: str = "") -> AgentRunRecord | None:
        record = self._runs.get(run_id)
        if record is None:
            return None
        registration = self._registry.get(run_id, kind="run")
        if registration is not None and registration.sealed:
            return record
        candidate = replace(record).complete(status, summary=summary, error=error)
        if not self._owns_record(record) or not self._refresh_runtime_lease():
            self._runs[run_id] = candidate
            self._registry.seal(run_id, kind="run")
            return candidate
        if self._swarm_store.upsert_agent_run(
            candidate.to_dict(),
            expected_owner_token=self._runtime_owner_token,
        ) is None:
            self._lease_lost = True
            self._runs[run_id] = candidate
            self._registry.seal(run_id, kind="run")
            return candidate
        self._runs[run_id] = candidate
        self._registry.seal(run_id, kind="run")
        self.write_metric("run_completed", candidate.to_dict())
        return candidate

    def start_subagent(
        self,
        *,
        subagent_id: str,
        parent_run_id: str = "",
        agent_type: str,
        prompt_summary: str = "",
        background: bool = False,
        task_id: str = "",
        objective: str = "",
        depends_on: list[str] | None = None,
        blocked_by: list[str] | None = None,
        cancel_with_parent: bool | None = None,
        detach_from_parent: bool | None = None,
        read_only: bool = False,
        write_scope: list[str] | None = None,
        current_activity: str = "",
    ) -> SubagentRunRecord:
        if not self._refresh_runtime_lease():
            raise RuntimeError("Agent runtime lease was lost; refusing to start a subagent.")
        existing = self._subagents.get(subagent_id)
        if existing is not None and existing.status == "running":
            raise RuntimeError(f"Subagent {subagent_id} is already running.")
        if subagent_id in self._subagent_slot_reservations:
            self._subagent_slot_reservations.discard(subagent_id)
        elif existing is None or existing.status != "running":
            active = sum(1 for item in self._subagents.values() if item.status == "running")
            if active + len(self._subagent_slot_reservations) >= MAX_CONCURRENT_SUBAGENTS:
                raise RuntimeError(
                    f"Maximum concurrent subagents reached ({MAX_CONCURRENT_SUBAGENTS})."
                )
        # Explicit background workers use an unlinked cancellation owner by
        # default. Foreground workers remain linked to the parent.
        if detach_from_parent is None and cancel_with_parent is None:
            detach = bool(background)
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
        parent_record = self._runs.get(str(parent_run_id or "").strip())
        parent_path = AgentPath.parse(parent_record.agent_path) if parent_record and parent_record.agent_path else AgentPath.main(parent_run_id or "main")
        previous_epoch = int(existing.mailbox_epoch) if existing is not None else 0
        record = SubagentRunRecord(
            subagent_id=subagent_id,
            parent_run_id=parent_run_id,
            agent_type=agent_type,
            prompt_summary=prompt_summary,
            background=background,
            task_id=task_id,
            objective=objective,
            depends_on=depends_on or [],
            blocked_by=blocked_by or [],
            cancel_with_parent=cancel_linked,
            detach_from_parent=detach,
            read_only=read_only,
            write_scope=write_scope or [],
            current_activity=current_activity,
            runtime_instance_id=self._runtime_instance_id,
            runtime_process_id=self._runtime_process_id,
            runtime_process_start_identity=self._runtime_process_start_identity,
            runtime_owner_token=self._runtime_owner_token,
            # Resuming/handoff may happen from a later parent turn. Keep the
            # original immutable path while allowing parent_run_id ownership to
            # move to the new active coordinator.
            agent_path=(existing.agent_path if existing and existing.agent_path else parent_path.child(subagent_id).value),
            mailbox_epoch=MailboxEpoch(previous_epoch).next().value,
        )
        persisted = self._swarm_store.upsert_subagent(
            record.to_dict(),
            expected_owner_token=self._runtime_owner_token,
            allow_takeover_terminal=True,
        )
        if persisted is None:
            raise RuntimeError(f"Subagent {subagent_id} is owned by another runtime.")
        if existing is not None:
            # Clear the prior incarnation only after the new ownership claim
            # succeeds. A failed cross-process claim must preserve its result.
            self._subagent_results.pop(subagent_id, None)
            self._swarm_store.delete_subagent_result(
                subagent_id,
                expected_owner_token=self._runtime_owner_token,
            )
            self._subagent_completion_events[subagent_id] = asyncio.Event()
        self._subagents[subagent_id] = record
        self._registry.register(record, kind="subagent")
        self._register_subagent_names(subagent_id, agent_type=agent_type, objective=objective, prompt_summary=prompt_summary)
        self._subagent_completion_events.setdefault(subagent_id, asyncio.Event())
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

    async def acquire_subagent_slot(
        self,
        subagent_id: str,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> bool:
        """Wait until one of Pi's four worker slots can be reserved.

        Parallel task batches may contain up to eight jobs.  Jobs beyond the
        active four wait here instead of being rejected or silently dropped.
        A per-waiter event avoids polling and cannot miss a release between the
        capacity check and waiter registration.
        """

        clean_id = str(subagent_id or "").strip()
        if not clean_id:
            return False
        while True:
            if self.try_reserve_subagent_slots([clean_id]):
                return True
            if cancel_event is not None and cancel_event.is_set():
                return False

            loop = asyncio.get_running_loop()
            event = asyncio.Event()
            waiter = (loop, event)
            self._subagent_capacity_waiters.add(waiter)
            try:
                # Recheck after registration so a slot released immediately
                # before the waiter was installed cannot strand this job.
                if self.try_reserve_subagent_slots([clean_id]):
                    return True
                if cancel_event is not None and cancel_event.is_set():
                    return False
                await event.wait()
            finally:
                self._subagent_capacity_waiters.discard(waiter)

    def _notify_subagent_capacity(self) -> None:
        for loop, event in tuple(self._subagent_capacity_waiters):
            if loop.is_closed():
                self._subagent_capacity_waiters.discard((loop, event))
                continue
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                self._subagent_capacity_waiters.discard((loop, event))

    def release_subagent_slot(self, subagent_id: str) -> None:
        self._subagent_slot_reservations.discard(str(subagent_id or "").strip())
        self._notify_subagent_capacity()

    def complete_subagent(
        self,
        subagent_id: str,
        status: AgentRunStatus = "completed",
        *,
        summary: str = "",
        tool_count: int = 0,
        agent_path: str = "",
        mailbox_epoch: int | None = None,
    ) -> SubagentRunRecord | None:
        record = self._subagents.get(subagent_id)
        if record is None:
            return None
        registration = self._registry.get(subagent_id, kind="subagent")
        if registration is not None and registration.sealed:
            if self._matches_subagent_incarnation(
                record,
                agent_path=agent_path,
                mailbox_epoch=mailbox_epoch,
            ):
                return record
            return None
        if not self._accepts_subagent_update(
            record,
            agent_path=agent_path,
            mailbox_epoch=mailbox_epoch,
        ):
            self._record_stale_subagent_update(
                subagent_id,
                operation="complete",
                agent_path=agent_path,
                mailbox_epoch=mailbox_epoch,
            )
            return None
        if not self._owns_record(record) or not self._refresh_runtime_lease():
            self._record_stale_subagent_update(
                subagent_id,
                operation="complete_owner",
                agent_path=agent_path,
                mailbox_epoch=mailbox_epoch,
            )
            return None
        candidate = replace(record).complete(status, summary=summary, tool_count=tool_count)
        if self._swarm_store.upsert_subagent(
            candidate.to_dict(),
            expected_owner_token=self._runtime_owner_token,
        ) is None:
            self._lease_lost = True
            self._record_stale_subagent_update(
                subagent_id,
                operation="complete_cas",
                agent_path=agent_path,
                mailbox_epoch=mailbox_epoch,
            )
            return None
        self._subagents[subagent_id] = candidate
        self._registry.seal(subagent_id, kind="subagent")
        self.write_metric("subagent_completed", candidate.to_dict())
        return candidate

    def register_subagent_task(
        self,
        subagent_id: str,
        task: asyncio.Task[Any],
        *,
        cancel_event: asyncio.Event | None = None,
        parent_run_id: str = "",
        owner_task_id: str = "",
        session_id: str = "",
        agent_type: str = "",
        prompt_summary: str = "",
        background: bool = False,
        pending: bool = False,
    ) -> None:
        self._subagent_tasks[subagent_id] = task
        self._subagent_task_metadata[subagent_id] = {
            "subagent_id": subagent_id,
            "parent_run_id": str(parent_run_id or "").strip(),
            "task_id": str(owner_task_id or "").strip(),
            "session_id": str(session_id or "").strip(),
            "agent_type": str(agent_type or "").strip(),
            "prompt_summary": str(prompt_summary or "").strip(),
            "objective": str(prompt_summary or "").strip(),
            "background": bool(background),
            "status": "pending" if pending else "running",
        }
        self._subagent_completion_events.setdefault(subagent_id, asyncio.Event())
        if cancel_event is not None:
            self._subagent_cancel_events[subagent_id] = cancel_event
        parent = str(parent_run_id or "").strip()
        if parent:
            self._subagent_parent_run_ids[subagent_id] = parent
        owner_task = str(owner_task_id or "").strip()
        if owner_task:
            self._subagent_owner_task_ids[subagent_id] = owner_task
        owner_session = str(session_id or "").strip()
        if owner_session:
            self._subagent_session_ids[subagent_id] = owner_session
        if not parent and not owner_task and not owner_session:
            self.write_metric(
                "subagent_task_registered_without_owner",
                {"subagent_id": subagent_id},
            )
        self.write_metric("subagent_task_registered", {"subagent_id": subagent_id})

    def mark_subagent_task_running(self, subagent_id: str) -> None:
        metadata = self._subagent_task_metadata.get(str(subagent_id or "").strip())
        if isinstance(metadata, dict):
            metadata["status"] = "running"

    def release_subagent_task(
        self,
        subagent_id: str,
        *,
        expected_task: asyncio.Task[Any] | None = None,
    ) -> bool:
        current_task = self._subagent_tasks.get(subagent_id)
        if expected_task is not None and current_task is not expected_task:
            self.write_metric(
                "subagent_stale_task_release_rejected",
                {"subagent_id": subagent_id},
            )
            return False
        self._subagent_tasks.pop(subagent_id, None)
        self._subagent_task_metadata.pop(subagent_id, None)
        self._subagent_cancel_events.pop(subagent_id, None)
        self._subagent_parent_run_ids.pop(subagent_id, None)
        self._subagent_owner_task_ids.pop(subagent_id, None)
        self._subagent_session_ids.pop(subagent_id, None)
        return True

    def get_subagent_task_metadata(self, subagent_id: str) -> dict[str, Any] | None:
        metadata = self._subagent_task_metadata.get(str(subagent_id or "").strip())
        return dict(metadata) if isinstance(metadata, dict) else None

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

    async def wait_for_any_subagent(
        self,
        subagent_ids: list[str],
        timeout: float,
    ) -> str | None:
        """Wait until any selected subagent publishes a durable result.

        A batch waiter should wake on useful mailbox activity instead of always
        waiting for the slowest child or the full timeout. This mirrors Codex's
        mailbox-style wait semantics and lets the coordinator react, steer, or
        collect completed work sooner.
        """

        clean_ids = list(dict.fromkeys(
            str(value or "").strip()
            for value in subagent_ids
            if str(value or "").strip()
        ))
        for subagent_id in clean_ids:
            if subagent_id in self._subagent_results:
                return subagent_id
        waiters = {
            asyncio.create_task(event.wait()): subagent_id
            for subagent_id in clean_ids
            if (event := self._subagent_completion_events.get(subagent_id)) is not None
        }
        if not waiters:
            return None
        done: set[asyncio.Task[bool]] = set()
        pending: set[asyncio.Task[bool]] = set(waiters)
        try:
            done, pending = await asyncio.wait(
                waiters,
                timeout=max(0.0, timeout),
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            subagent_id = waiters[task]
            if subagent_id in self._subagent_results:
                return subagent_id
        return None

    def cancel_subagent_task(self, subagent_id: str) -> Literal["cancelled", "done", "not_found"]:
        task = self._subagent_tasks.get(subagent_id)
        if task is None:
            return "not_found"
        cancel_event = self._subagent_cancel_events.get(subagent_id)
        if cancel_event is not None:
            cancel_event.set()
        if task.done():
            self.release_subagent_task(subagent_id, expected_task=task)
            return "done"
        task.cancel()
        self.write_metric("subagent_task_cancel_requested", {"subagent_id": subagent_id})
        return "cancelled"

    def cancel_child_subagent_tasks(
        self,
        parent_run_id: str,
        *,
        reason: str = "parent_cancelled",
        force: bool = False,
    ) -> list[str]:
        parent = str(parent_run_id or "").strip()
        if not parent:
            return []
        cancelled: list[str] = []
        for subagent_id, recorded_parent in list(self._subagent_parent_run_ids.items()):
            if recorded_parent != parent:
                continue
            record = self._subagents.get(subagent_id)
            if record is not None:
                if not force and bool(getattr(record, "detach_from_parent", False)):
                    continue
                if not force and not bool(getattr(record, "cancel_with_parent", True)):
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
            for subagent_id in self.cancel_child_subagent_tasks(parent_run_id, reason=reason, force=True):
                if subagent_id in seen:
                    continue
                seen.add(subagent_id)
                cancelled.append(subagent_id)
        # Compatibility/embedding fallback: a child created without a run id
        # is still owned by the user-visible task that launched it.
        for subagent_id, owner_task_id in list(self._subagent_owner_task_ids.items()):
            if owner_task_id != task or subagent_id in seen:
                continue
            status = self.cancel_subagent_task(subagent_id)
            if status in {"cancelled", "done"}:
                seen.add(subagent_id)
                cancelled.append(subagent_id)
        return cancelled

    def cancel_subagent_tasks_for_session(
        self,
        session_id: str,
        *,
        reason: str = "session_shutdown",
    ) -> list[str]:
        """Force-cancel every child task owned by a closing client session."""
        session = str(session_id or "").strip()
        if not session:
            return []
        cancelled: list[str] = []
        for subagent_id, owner_session in list(self._subagent_session_ids.items()):
            if owner_session != session:
                continue
            status = self.cancel_subagent_task(subagent_id)
            if status in {"cancelled", "done"}:
                cancelled.append(subagent_id)
        if cancelled:
            self.write_metric(
                "subagent_session_cancel_requested",
                {
                    "session_id": session,
                    "subagent_ids": cancelled,
                    "reason": reason,
                },
            )
        return cancelled

    def get_subagent(self, subagent_id: str) -> SubagentRunRecord | None:
        return self._subagents.get(subagent_id)

    def accepts_parent_notification(
        self,
        *,
        subagent_id: str,
        mailbox_epoch: int,
        parent_run_id: str = "",
    ) -> bool:
        """Return whether a durable mailbox item belongs to this incarnation."""
        if self._registry.accepts_mailbox(
            subagent_id=subagent_id,
            mailbox_epoch=mailbox_epoch,
            parent_run_id=parent_run_id,
        ):
            return True
        # A background child can finish after its original parent turn ended,
        # including when process recovery sealed both records. A later parent
        # turn in the same conversation may consume that terminal notification
        # while the mailbox epoch still matches. Reusing the subagent id bumps
        # the epoch, so stale-incarnation results remain rejected.
        record = self._subagents.get(str(subagent_id or "").strip())
        if record is None or record.status == "running":
            return False
        if int(mailbox_epoch or 0) != int(record.mailbox_epoch or 0):
            return False
        target_parent = self._runs.get(str(parent_run_id or "").strip())
        original_parent = self._runs.get(str(record.parent_run_id or "").strip())
        if target_parent is None or original_parent is None:
            return False
        if original_parent.status == "running":
            return False
        target_conversation = str(target_parent.conversation_id or "").strip()
        original_conversation = str(original_parent.conversation_id or "").strip()
        return bool(target_conversation and target_conversation == original_conversation)

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
        terminal_reason: str = "",
        artifact_id: str = "",
        usage: dict[str, Any] | None = None,
        agent_path: str = "",
        mailbox_epoch: int | None = None,
    ) -> SubagentResultRecord | None:
        run_record = self._subagents.get(subagent_id)
        registration = self._registry.get(subagent_id, kind="subagent")
        incarnation_matches = bool(
            run_record is not None
            and self._matches_subagent_incarnation(
                run_record,
                agent_path=agent_path,
                mailbox_epoch=mailbox_epoch,
            )
        )
        update_is_live = bool(
            run_record is not None
            and registration is not None
            and (
                registration.sealed
                or self._accepts_subagent_update(
                    run_record,
                    agent_path=agent_path,
                    mailbox_epoch=mailbox_epoch,
                )
            )
        )
        if not incarnation_matches or not update_is_live:
            self._record_stale_subagent_update(
                subagent_id,
                operation="result",
                agent_path=agent_path,
                mailbox_epoch=mailbox_epoch,
            )
            return None
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
            terminal_reason=str(terminal_reason or ""),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            artifact_id=str(artifact_id or ""),
            agent_path=run_record.agent_path,
            mailbox_epoch=run_record.mailbox_epoch,
            runtime_owner_token=self._runtime_owner_token,
        )
        # Durability precedes observability: once a waiter sees completion, the
        # result must already survive a process restart.
        if not self._owns_record(run_record) or not self._refresh_runtime_lease():
            self._record_stale_subagent_update(
                subagent_id,
                operation="result_owner",
                agent_path=agent_path,
                mailbox_epoch=mailbox_epoch,
            )
            return None
        if self._swarm_store.upsert_subagent_result(
            record.to_dict(),
            expected_owner_token=self._runtime_owner_token,
            agent_path=run_record.agent_path,
            mailbox_epoch=run_record.mailbox_epoch,
        ) is None:
            self._record_stale_subagent_update(
                subagent_id,
                operation="result_cas",
                agent_path=agent_path,
                mailbox_epoch=mailbox_epoch,
            )
            return None
        self._subagent_results[subagent_id] = record
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

    @staticmethod
    def _matches_subagent_incarnation(
        record: SubagentRunRecord,
        *,
        agent_path: str,
        mailbox_epoch: int | None,
    ) -> bool:
        if mailbox_epoch is None or not str(agent_path or "").strip():
            return False
        return (
            str(agent_path).strip() == str(record.agent_path or "").strip()
            and int(mailbox_epoch) == int(record.mailbox_epoch or 0)
        )

    def accepts_subagent_incarnation(
        self,
        subagent_id: str,
        *,
        agent_path: str,
        mailbox_epoch: int | None,
        require_running: bool = False,
    ) -> bool:
        """Validate an immutable child-incarnation fence for async callbacks."""
        record = self._subagents.get(str(subagent_id or "").strip())
        if record is None:
            return False
        if require_running and record.status != "running":
            return False
        return self._matches_subagent_incarnation(
            record,
            agent_path=agent_path,
            mailbox_epoch=mailbox_epoch,
        )

    def _accepts_subagent_update(
        self,
        record: SubagentRunRecord,
        *,
        agent_path: str,
        mailbox_epoch: int | None,
    ) -> bool:
        if not self._matches_subagent_incarnation(
            record,
            agent_path=agent_path,
            mailbox_epoch=mailbox_epoch,
        ):
            return False
        registration = self._registry.get(record.subagent_id, kind="subagent")
        if registration is None:
            return False
        return self._registry.accepts_update(
            agent_id=record.subagent_id,
            kind="subagent",
            agent_path=record.agent_path,
            mailbox_epoch=record.mailbox_epoch,
        )

    def _record_stale_subagent_update(
        self,
        subagent_id: str,
        *,
        operation: str,
        agent_path: str,
        mailbox_epoch: int | None,
    ) -> None:
        current = self._subagents.get(subagent_id)
        self.write_metric(
            "subagent_stale_update_rejected",
            {
                "subagent_id": subagent_id,
                "operation": operation,
                "received_agent_path": str(agent_path or ""),
                "received_mailbox_epoch": mailbox_epoch,
                "current_agent_path": str(getattr(current, "agent_path", "") or ""),
                "current_mailbox_epoch": int(getattr(current, "mailbox_epoch", 0) or 0),
            },
        )

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
        task_metadata = self._subagent_task_metadata.get(subagent_id, {})
        payload: dict[str, Any] = (
            record.to_dict()
            if record is not None
            else {"subagent_id": subagent_id, **task_metadata}
        )
        if task is not None:
            task_status = str(task_metadata.get("status") or "running")
            payload["background_task"] = (
                "done"
                if task.done()
                else "running"
                if record is not None or task_status == "running"
                else "queued"
            )
            payload.setdefault("status", task_status)
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
        record = self._subagents.get(subagent_id)
        if record is not None and not self._owns_record(record):
            self.write_metric(
                "subagent_stale_update_rejected",
                {"subagent_id": subagent_id, "operation": "forget_result"},
            )
            return False
        if not self._refresh_runtime_lease():
            return False
        removed_store = self._swarm_store.delete_subagent_result(
            subagent_id,
            expected_owner_token=self._runtime_owner_token,
        )
        removed_memory = self._subagent_results.pop(subagent_id, None) is not None
        self._subagent_completion_events.pop(subagent_id, None)
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
            "artifact_id": result.artifact_id,
            "completed_at": result.completed_at,
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
            mailbox_epoch=int(getattr(record, "mailbox_epoch", 0) or 0) if record else 0,
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

    def mark_parent_notification_delivered(
        self,
        notification_id: str,
        *,
        parent_run_id: str = "",
        conversation_id: str = "",
    ) -> dict[str, Any] | None:
        item = self.parent_outbox(
            parent_run_id=parent_run_id,
            conversation_id=conversation_id,
        ).mark_delivered(notification_id)
        return item.to_dict() if item is not None else None

    def mark_parent_notification_failed(
        self,
        notification_id: str,
        error: str,
        *,
        parent_run_id: str = "",
        conversation_id: str = "",
    ) -> dict[str, Any] | None:
        item = self.parent_outbox(
            parent_run_id=parent_run_id,
            conversation_id=conversation_id,
        ).mark_failed(notification_id, error)
        return item.to_dict() if item is not None else None

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
        sender_mailbox_epoch: int | None = None,
        recipient_mailbox_epoch: int | None = None,
    ) -> SwarmMessageRecord:
        sender_record = self.get_subagent(sender_id)
        sender_run = self.get_run(sender_id)
        recipient_record = self.get_subagent(recipient_id)
        if sender_record is not None and str(sender_record.status or "") not in {"running"}:
            raise ValueError(
                f"cannot send mailbox messages from sealed subagent {sender_id} "
                f"with status {sender_record.status}"
            )
        if sender_run is not None and str(sender_run.status or "") != "running":
            raise ValueError(
                f"cannot send mailbox messages from sealed run {sender_id} "
                f"with status {sender_run.status}"
            )
        if sender_record is not None:
            if sender_mailbox_epoch is None:
                raise ValueError(
                    f"sender mailbox epoch is required for subagent {sender_id}"
                )
            resolved_sender_epoch = max(0, int(sender_mailbox_epoch))
            if resolved_sender_epoch != int(sender_record.mailbox_epoch or 0):
                raise ValueError(
                    f"stale sender incarnation for {sender_id}: "
                    f"received epoch {resolved_sender_epoch}, current epoch {sender_record.mailbox_epoch}"
                )
        else:
            resolved_sender_epoch = max(0, int(sender_mailbox_epoch or 0))
        expected_recipient_epoch = (
            max(0, int(getattr(recipient_record, "mailbox_epoch", 0) or 0)) + 1
            if recipient_record is not None and str(recipient_record.status or "") != "running"
            else max(0, int(getattr(recipient_record, "mailbox_epoch", 0) or 0))
        )
        resolved_recipient_epoch = (
            max(0, int(recipient_mailbox_epoch))
            if recipient_mailbox_epoch is not None
            else expected_recipient_epoch
        )
        if recipient_record is not None and resolved_recipient_epoch != expected_recipient_epoch:
            raise ValueError(
                f"recipient incarnation mismatch for {recipient_id}: "
                f"received epoch {resolved_recipient_epoch}, expected epoch {expected_recipient_epoch}"
            )
        broadcast_epochs: dict[str, int] = {}
        if recipient_id in {"all", "*"}:
            for participant_id, participant in self._subagents.items():
                parent = self._runs.get(str(participant.parent_run_id or "").strip())
                participant_conversation = str(getattr(parent, "conversation_id", "") or "")
                if conversation_id and participant_conversation != conversation_id:
                    continue
                broadcast_epochs[participant_id] = int(participant.mailbox_epoch or 0)
        record = _swarm_message_from_dict(
            self._swarm_store.append_message({
                "sender_id": sender_id,
                "recipient_id": recipient_id,
                "content": content,
                "conversation_id": conversation_id,
                "team_name": team_name,
                "task_id": task_id,
                "message_id": message_id,
                "sender_mailbox_epoch": resolved_sender_epoch,
                "recipient_mailbox_epoch": resolved_recipient_epoch,
                "recipient_mailbox_epochs": broadcast_epochs,
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
        mailbox_epoch: int | None = None,
    ) -> list[SwarmMessageRecord]:
        bounded_limit = max(1, min(int(limit or 20), 100))
        records = [
            _swarm_message_from_dict(item)
            for item in self._swarm_store.list_messages(
                participant_id=participant_id,
                conversation_id=conversation_id,
                since_seq=since_seq,
                limit=100 if mailbox_epoch is not None and participant_id else bounded_limit,
            )
        ]
        if mailbox_epoch is not None and participant_id:
            current_epoch = max(0, int(mailbox_epoch or 0))
            participant = str(participant_id or "").strip()
            participant_record = self.get_subagent(participant)
            started_at = int(getattr(participant_record, "started_at", 0) or 0)

            def belongs_to_incarnation(message: SwarmMessageRecord) -> bool:
                if message.recipient_id in {"all", "*"}:
                    target_epoch = message.recipient_mailbox_epochs.get(participant)
                    if target_epoch is not None:
                        return int(target_epoch) == current_epoch
                    # Legacy broadcasts had no recipient snapshot. They are
                    # visible only to an initial incarnation and never cross a
                    # restart boundary.
                    return current_epoch <= 1 and (not started_at or message.created_at >= started_at)
                if message.recipient_id == participant:
                    target_epoch = int(message.recipient_mailbox_epoch or 0)
                    return target_epoch == current_epoch or (target_epoch == 0 and current_epoch <= 1)
                if message.sender_id == participant:
                    source_epoch = int(message.sender_mailbox_epoch or 0)
                    return source_epoch == current_epoch or (source_epoch == 0 and current_epoch <= 1)
                return False

            records = [message for message in records if belongs_to_incarnation(message)][-bounded_limit:]
        for record in records:
            self._swarm_messages[record.message_id] = record
        return records

    def claim_swarm_messages(
        self,
        *,
        participant_id: str,
        mailbox_epoch: int,
        conversation_id: str = "",
        since_seq: int = 0,
        limit: int = 100,
        lease_ms: int = 30_000,
    ) -> list[MailboxMessageClaim]:
        if not self._refresh_runtime_lease():
            raise RuntimeError("runtime lease was lost before mailbox claim")
        raw_claims = self._swarm_store.claim_messages(
            participant_id=participant_id,
            mailbox_epoch=mailbox_epoch,
            claim_owner=self._runtime_owner_token,
            conversation_id=conversation_id,
            since_seq=since_seq,
            limit=limit,
            lease_ms=lease_ms,
        )
        claims: list[MailboxMessageClaim] = []
        for item in raw_claims:
            message = _swarm_message_from_dict(dict(item.get("message") or {}))
            self._swarm_messages[message.message_id] = message
            claims.append(MailboxMessageClaim(
                message=message,
                participant_id=str(item.get("participant_id") or participant_id),
                mailbox_epoch=max(0, int(item.get("mailbox_epoch") or mailbox_epoch)),
                claim_token=str(item.get("claim_token") or ""),
                lease_expires_at=int(item.get("lease_expires_at") or 0),
            ))
        return claims

    def ack_swarm_message_claims(self, claims: list[MailboxMessageClaim]) -> int:
        return self._swarm_store.ack_message_claims(
            [claim.claim_ref() for claim in claims],
            claim_owner=self._runtime_owner_token,
        )

    def release_swarm_message_claims(self, claims: list[MailboxMessageClaim]) -> int:
        return self._swarm_store.release_message_claims(
            [claim.claim_ref() for claim in claims],
            claim_owner=self._runtime_owner_token,
        )

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
        agent_type: str = "general-purpose",
        role: str = "",
        objective: str = "",
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
                "agent_type": agent_type,
                "role": role,
                "objective": objective,
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
            subagents = [
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
            known_ids = {str(item.get("subagent_id") or "") for item in subagents}
            subagents.extend(
                {
                    **metadata,
                    "background_task": "done" if task.done() else "queued",
                    "result_available": False,
                }
                for subagent_id, task in self._subagent_tasks.items()
                if subagent_id not in known_ids
                and isinstance((metadata := self._subagent_task_metadata.get(subagent_id)), dict)
                and (not conversation_id or str(metadata.get("parent_run_id") or "") in parent_ids)
            )
            payload["subagents"] = subagents
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


_DEFAULT_RUNTIME_LOCK = threading.Lock()
_DEFAULT_RUNTIME = AgentRuntime()


def default_runtime() -> AgentRuntime:
    """Return a live process runtime, rebuilding after an orderly shutdown.

    FastAPI lifespan can be entered more than once in one interpreter (tests,
    embedded desktop restart, hot reload). ``close(release_lease=True)`` fences
    the old object permanently, so handing it out again would make every new
    turn fail with ``lease was lost``.
    """

    global _DEFAULT_RUNTIME
    if not _DEFAULT_RUNTIME._lease_lost:
        return _DEFAULT_RUNTIME
    with _DEFAULT_RUNTIME_LOCK:
        if _DEFAULT_RUNTIME._lease_lost:
            _DEFAULT_RUNTIME = AgentRuntime()
        return _DEFAULT_RUNTIME
