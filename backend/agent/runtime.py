"""Runtime records and metrics for MiniCode's agent control plane.

This module is deliberately small: the existing ReAct loop remains the
execution kernel, while AgentRuntime gives WebSocket/UI layers one stable
shape for runs, phases, subagents, checkpoints, and local observability.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Literal
from uuid import uuid4

from backend.async_cleanup import (
    CANCELLATION_DRAIN_TIMEOUT_SECONDS,
    cancel_and_drain,
    cancel_and_drain_to_completion,
)
from backend.config import DATA_ROOT, TokenBudget
from backend.agent.runtime_records import (
    AgentRunStatus,
    AgentRunPhase,
    SwarmTaskStatus,
    epoch_ms,
    new_run_id,
    AgentRunRecord,
    SubagentRunRecord,
    SubagentResultRecord,
    SwarmMessageRecord,
    MailboxMessageClaim,
    SwarmTaskOutputRecord,
    SwarmTaskRecord,
    SwarmTeamMemberRecord,
    SwarmTeamRecord,
    RunCheckpoint,
    budget_snapshot,
    _swarm_message_from_dict,
    _swarm_task_output_from_dict,
    _string_list,
    _swarm_task_from_dict,
    _subagent_from_dict,
    _agent_run_from_dict,
    _subagent_result_from_dict,
    _swarm_team_member_from_dict,
    _swarm_team_from_dict,
)

from backend.agent.execution_journal import (
    ExecutionJournal,
    delete_agent_journal,
    load_agent_transcript,
)
from backend.agent.parent_notification_outbox import (
    ParentNotification,
    ParentNotificationOutbox,
    enqueue_parent_notification,
    load_parent_outbox,
)
from backend.agent.agent_identity import AgentPath, MailboxEpoch
from backend.agent.agent_registry import AgentRegistry
from backend.agent.public_projection import (
    project_public_agent_run,
    project_public_metric_payload,
    project_public_subagent_result,
    project_public_subagent_run,
    project_public_swarm_message,
    project_public_swarm_task,
    project_public_swarm_task_output,
    project_public_swarm_team,
    project_public_swarm_team_member,
    project_public_usage,
)
from backend.agent.swarm_store import FileSwarmStore, MAILBOX_MESSAGE_LEASE_MS
from backend.runtime_paths import agent_runtime_root

logger = logging.getLogger(__name__)

# Metrics are an observational JSONL feed. Durable run/subagent authority is
# committed in the swarm SQLite store before these rows are emitted, so the
# telemetry path must not put a synchronous fsync on every lifecycle callback.
# The process-local lock removes duplicate work inside one runtime. The
# complete batch is emitted with one O_APPEND write, so a late callback from an
# expired owner cannot splice half a JSONL record into the replacement runtime's
# observation stream. A lost metric never changes run authority and is reported
# through ``metric_persistence`` when it is observable.
_METRIC_APPEND_LOCK = threading.RLock()



class TerminalCommitError(RuntimeError):
    """Raised when a run terminal cannot be durably committed.

    A terminal candidate is never authoritative until the owner/lease fence and
    the durable CAS both accept it.  Keeping this error distinct from ordinary
    provider/runtime failures lets the canonical lifecycle surface an explicit
    ``terminal_commit_failed`` fact instead of emitting a false success.
    """

    def __init__(self, run_id: str, failure_kind: str, message: str) -> None:
        self.run_id = str(run_id or "")
        self.failure_kind = str(failure_kind or "persistence_failed")
        super().__init__(message)

# ---------------------------------------------------------------------------
# Explicit four-type Agent taxonomy (plan §11.2)
# ---------------------------------------------------------------------------

AgentRole = Literal["primary", "subagent", "side_query", "background"]

AGENT_ROLES: frozenset[str] = frozenset({"primary", "subagent", "side_query", "background"})

# MiniCode executes up to four independent bounded workers in parallel.
# Lifetime is governed by persisted run and cancellation records;
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
        self._metric_write_failures: deque[dict[str, Any]] = deque(maxlen=64)
        # Metric appends are one locked O_APPEND write each. Reconciliation can
        # emit a thousand of them in a burst, so batching collapses that into a
        # single append instead of paying lock/open/write/close per record.
        self._metric_batch: list[dict[str, Any]] | None = None
        self._metric_batch_depth = 0
        self._metric_batch_guard = threading.Lock()
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
        self._runtime_state_root = (
            store_dir.parent
            if metrics_file is not None or swarm_store_dir is not None
            else agent_runtime_root()
        )
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
        self._execution_journal_lock = threading.Lock()
        self._execution_journals: dict[str, ExecutionJournal] = {}
        self._runs: dict[str, AgentRunRecord] = {}
        self._subagents: dict[str, SubagentRunRecord] = {}
        self._registry = AgentRegistry()
        self._subagent_tasks: dict[str, asyncio.Task[Any]] = {}
        self._subagent_task_metadata: dict[str, dict[str, Any]] = {}
        self._subagent_slot_reservations: set[str] = set()
        self._subagent_capacity_waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()
        self._subagent_cancel_events: dict[str, asyncio.Event] = {}
        self._subagent_completion_events: dict[str, asyncio.Event] = {}
        # One provider-neutral activity feed powers every wait and lifecycle
        # projection. Waiters receive only bounded
        # metadata (kind/ids/sequence), never mailbox contents or provider raw
        # payloads.
        self._agent_activity_lock = threading.Lock()
        self._agent_activity_seq = 0
        self._agent_activity_log: deque[dict[str, Any]] = deque(maxlen=2048)
        self._agent_activity_waiters: set[
            tuple[asyncio.AbstractEventLoop, asyncio.Event]
        ] = set()
        self._subagent_parent_run_ids: dict[str, str] = {}
        # A task/session ownership fence is retained even when an embedder did
        # not provide a parent run id. Explicit user cancellation and session
        # shutdown must still be able to find and stop the child.
        self._subagent_owner_task_ids: dict[str, str] = {}
        self._subagent_session_ids: dict[str, str] = {}
        self._subagent_results: dict[str, SubagentResultRecord] = {}
        # name -> subagent_id registry for by-name addressing. Latest wins.
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
        self._reconcile_recovered_cleanup(active_owner_tokens=active_owner_tokens)
        heartbeat_enabled = (
            metrics_file is None and swarm_store_dir is None
            if enable_lease_heartbeat is None
            else bool(enable_lease_heartbeat)
        )
        if heartbeat_enabled:
            self._start_lease_heartbeat()

    def _reconcile_recovered_cleanup(
        self,
        *,
        active_owner_tokens: set[str],
    ) -> None:
        """Take over dead-owner cleanup intents, then reconcile resources."""

        with self.batched_metrics():
            self._reconcile_recovered_cleanup_locked(
                active_owner_tokens=active_owner_tokens
            )

    def _reconcile_recovered_cleanup_locked(
        self,
        *,
        active_owner_tokens: set[str],
    ) -> None:
        active_owners = {
            str(owner or "").strip() for owner in active_owner_tokens if str(owner or "").strip()
        }

        def may_reconcile(owner_token: str) -> bool:
            owner = str(owner_token or "").strip()
            return owner == self._runtime_owner_token or owner not in active_owners

        for kind, records in (("run", self._runs), ("subagent", self._subagents)):
            for record_id, record in list(records.items()):
                if not record.cleanup_pending or not may_reconcile(record.runtime_owner_token):
                    continue
                previous_owner = str(record.runtime_owner_token or "").strip()
                if previous_owner == self._runtime_owner_token:
                    continue
                candidate = replace(
                    record,
                    runtime_instance_id=self._runtime_instance_id,
                    runtime_process_id=self._runtime_process_id,
                    runtime_process_start_identity=self._runtime_process_start_identity,
                    runtime_owner_token=self._runtime_owner_token,
                )
                if kind == "run":
                    persisted = self._swarm_store.upsert_agent_run(
                        candidate.to_dict(),
                        expected_owner_token=previous_owner,
                        allow_takeover_terminal=True,
                    )
                else:
                    persisted = self._swarm_store.upsert_subagent(
                        candidate.to_dict(),
                        expected_owner_token=previous_owner,
                        allow_takeover_terminal=True,
                    )
                if persisted is None:
                    logger.warning("Cleanup ownership takeover lost the %s fence for %s", kind, record_id)
                    continue
                records[record_id] = candidate

        self._reconcile_recovered_cleanup_resources()

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
                    logger.warning("Agent runtime lease heartbeat failed", exc_info=True)
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

    def _parent_agent_path(self, parent_run_id: str) -> AgentPath:
        clean_parent_id = str(parent_run_id or "").strip()
        parent_record = (
            self._runs.get(clean_parent_id)
            or self._subagents.get(clean_parent_id)
        )
        if parent_record is not None and parent_record.agent_path:
            return AgentPath.parse(parent_record.agent_path)
        return AgentPath.main(clean_parent_id or "main")

    def _agent_path_owner(
        self,
        agent_path: str,
        *,
        excluding_subagent_id: str = "",
    ) -> str:
        candidate = str(agent_path or "").strip()
        excluded = str(excluding_subagent_id or "").strip()
        if not candidate:
            return ""
        for record in self._subagents.values():
            if record.subagent_id != excluded and str(record.agent_path or "") == candidate:
                return record.subagent_id
        for subagent_id, metadata in self._subagent_task_metadata.items():
            if subagent_id == excluded or not isinstance(metadata, dict):
                continue
            if str(metadata.get("agent_path") or "") == candidate:
                return subagent_id
        return ""

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
        mailbox_epoch: int = 0,
    ) -> AgentRunRecord:
        if not self._refresh_runtime_lease():
            raise RuntimeError("Agent runtime lease was lost; refusing to start a new run.")
        resolved_run_id = run_id or new_run_id()
        existing = self._runs.get(resolved_run_id)
        if existing is not None and existing.status == "running":
            if (
                existing.runtime_owner_token == self._runtime_owner_token
                and str(existing.parent_run_id or "") == str(parent_run_id or "")
                and str(existing.role or "") == str(role or "")
                and str(existing.task_id or "") == str(task_id or "")
                and str(existing.session_id or "") == str(session_id or "")
                and int(existing.mailbox_epoch or 0) == max(0, int(mailbox_epoch or 0))
            ):
                # Admission may create the durable record before provider and
                # extension setup. QueryEngine reuses that exact owner instead
                # of creating a competing run for the same scheduled turn.
                return existing
            raise RuntimeError(f"Agent run {resolved_run_id} is already running.")
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
            mailbox_epoch=max(0, int(mailbox_epoch or 0)),
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

    def complete_run(
        self,
        run_id: str,
        status: AgentRunStatus = "completed",
        *,
        summary: str = "",
        terminal_reason: str = "",
        error: str = "",
    ) -> AgentRunRecord | None:
        try:
            return self.commit_terminal(
                run_id,
                status,
                summary=summary,
                terminal_reason=terminal_reason,
                error=error,
            )
        except TerminalCommitError as exc:
            # A failed terminal commit is durable evidence, never a silent
            # None: callers must be able to tell "already sealed" from
            # "persistence rejected the commit".
            logger.error(
                "complete_run(%s, %s) failed: %s",
                run_id,
                status,
                exc,
            )
            self.write_metric(
                "terminal_commit_failed",
                {
                    "run_id": run_id,
                    "status": status,
                    "reason": getattr(exc, "reason", "") or "terminal_commit_error",
                    "detail": str(exc),
                },
            )
            return None

    def commit_terminal(
        self,
        run_id: str,
        status: AgentRunStatus = "completed",
        *,
        summary: str = "",
        terminal_reason: str = "",
        error: str = "",
    ) -> AgentRunRecord:
        """Commit a terminal record or raise without mutating local state.

        This is the canonical terminal boundary.  ``complete_run`` remains a
        compatibility method for callers that historically accepted ``None``;
        lifecycle owners must use this strict API so a lost lease/CAS cannot be
        mistaken for a durable completion.
        """
        record = self._runs.get(run_id)
        if record is None:
            raise TerminalCommitError(
                run_id,
                "missing_run",
                f"Cannot commit terminal for unknown run {run_id!r}",
            )
        registration = self._registry.get(run_id, kind="run")
        if registration is not None and registration.sealed:
            if record.status != "running":
                return record
            raise TerminalCommitError(
                run_id,
                "already_sealed",
                f"Run {run_id!r} is sealed before terminal commit",
            )
        try:
            owns_record = self._owns_record(record)
            lease_ok = self._refresh_runtime_lease()
        except Exception as exc:
            self._lease_lost = True
            raise TerminalCommitError(
                run_id,
                "lease_check_failed",
                f"Could not verify runtime lease for {run_id!r}",
            ) from exc
        if not owns_record or not lease_ok:
            self._lease_lost = True
            raise TerminalCommitError(
                run_id,
                "lease_lost",
                f"Runtime lease lost while committing terminal for {run_id!r}",
            )
        candidate = replace(record).complete(
            status,
            summary=summary,
            terminal_reason=terminal_reason,
            error=error,
        )
        try:
            persisted = self._swarm_store.upsert_agent_run(
                candidate.to_dict(),
                expected_owner_token=self._runtime_owner_token,
            )
        except Exception as exc:
            self._lease_lost = True
            raise TerminalCommitError(
                run_id,
                "persistence_error",
                f"Durable terminal write failed for {run_id!r}",
            ) from exc
        if persisted is None:
            self._lease_lost = True
            raise TerminalCommitError(
                run_id,
                "cas_rejected",
                f"Durable terminal CAS rejected for {run_id!r}",
            )
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
        session_id: str = "",
        objective: str = "",
        depends_on: list[str] | None = None,
        blocked_by: list[str] | None = None,
        cancel_with_parent: bool | None = None,
        detach_from_parent: bool | None = None,
        read_only: bool = False,
        write_scope: list[str] | None = None,
        resume_config: dict[str, Any] | None = None,
        current_activity: str = "",
        teammate_name: str = "",
        team_name: str = "",
        permission_mode: str = "confirm",
        plan_mode_required: bool = False,
        agent_path_segment: str = "",
    ) -> SubagentRunRecord:
        if not self._refresh_runtime_lease():
            raise RuntimeError("Agent runtime lease was lost; refusing to start a subagent.")
        existing = self._subagents.get(subagent_id)
        if existing is None:
            persisted_existing = self._swarm_store.get_subagent(subagent_id)
            if persisted_existing is not None:
                existing = _subagent_from_dict(persisted_existing)
        if existing is not None and existing.status == "running":
            raise RuntimeError(f"Subagent {subagent_id} is already running.")
        if subagent_id in self._subagent_slot_reservations:
            self._subagent_slot_reservations.discard(subagent_id)
        elif existing is None or existing.status != "running":
            if not teammate_name:
                # Bounded workers share the global capacity; named teammates
                # have a separate lifecycle and no bounded-worker ceiling.
                active = sum(
                    1
                    for item in self._subagents.values()
                    if item.status == "running" and not item.teammate_name
                )
                reserved = sum(
                    1
                    for subagent_id in self._subagent_slot_reservations
                    if not (self._subagents.get(subagent_id) and self._subagents[subagent_id].teammate_name)
                )
                if active + reserved >= MAX_CONCURRENT_SUBAGENTS:
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
        clean_parent_run_id = str(parent_run_id or "").strip()
        # A hierarchical child can itself be the parent of another child. Run
        # and subagent records share this one canonical path resolver.
        parent_path = self._parent_agent_path(clean_parent_run_id)
        previous_epoch = int(existing.mailbox_epoch) if existing is not None else 0
        candidate_agent_path = (
            existing.agent_path
            if existing and existing.agent_path
            else parent_path.child(agent_path_segment or subagent_id).value
        )
        path_owner = self._agent_path_owner(
            candidate_agent_path,
            excluding_subagent_id=subagent_id,
        )
        if path_owner:
            raise RuntimeError(
                f"Agent path {candidate_agent_path!r} is already owned by {path_owner}."
            )
        record = SubagentRunRecord(
            subagent_id=subagent_id,
            parent_run_id=parent_run_id,
            agent_type=agent_type,
            prompt_summary=prompt_summary,
            background=background,
            task_id=task_id,
            session_id=str(session_id or "").strip(),
            objective=objective,
            depends_on=depends_on or [],
            blocked_by=blocked_by or [],
            cancel_with_parent=cancel_linked,
            detach_from_parent=detach,
            read_only=read_only,
            write_scope=write_scope or [],
            resume_config=dict(resume_config or {}),
            current_activity=current_activity,
            runtime_instance_id=self._runtime_instance_id,
            runtime_process_id=self._runtime_process_id,
            runtime_process_start_identity=self._runtime_process_start_identity,
            runtime_owner_token=self._runtime_owner_token,
            # Resuming/handoff may happen from a later parent turn. Keep the
            # original immutable path while allowing parent_run_id ownership to
            # move to the new active coordinator.
            agent_path=candidate_agent_path,
            mailbox_epoch=MailboxEpoch(previous_epoch).next().value,
            teammate_name=str(teammate_name or "").strip(),
            team_name=str(team_name or "").strip(),
            permission_mode=str(permission_mode or "confirm").strip(),
            plan_mode_required=bool(plan_mode_required),
            awaiting_plan_approval=False,
            active_plan_request_id="",
            is_idle=False,
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
        self._register_subagent_names(
            subagent_id,
            teammate_name=record.teammate_name,
            task_name=record.task_id,
        )
        if subagent_id not in self._subagent_completion_events:
            self._subagent_completion_events[subagent_id] = asyncio.Event()
        self.write_metric("subagent_started", record.to_dict())
        self._record_agent_activity(
            "started",
            agent_ids=(subagent_id,),
            conversation_id=self._conversation_id_for_agent(subagent_id),
            status=record.status,
        )
        return record

    def try_reserve_subagent_slots(self, subagent_ids: list[str]) -> bool:
        clean_ids = list(dict.fromkeys(str(value or "").strip() for value in subagent_ids if str(value or "").strip()))
        teammate_ids = {
            subagent_id
            for subagent_id, item in self._subagents.items()
            if item.teammate_name
        }
        active = sum(
            1
            for item in self._subagents.values()
            if item.status == "running" and not item.teammate_name
        )
        needed = sum(
            1
            for subagent_id in clean_ids
            if subagent_id not in teammate_ids
            and subagent_id not in self._subagent_slot_reservations
            and not (self._subagents.get(subagent_id) and self._subagents[subagent_id].status == "running")
        )
        reserved = sum(
            1 for subagent_id in self._subagent_slot_reservations if subagent_id not in teammate_ids
        )
        if active + reserved + needed > MAX_CONCURRENT_SUBAGENTS:
            return False
        self._subagent_slot_reservations.update(clean_ids)
        return True

    async def acquire_subagent_slot(
        self,
        subagent_id: str,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> bool:
        """Wait until one of MiniCode's bounded worker slots can be reserved.

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
        self._record_agent_activity(
            "completed",
            agent_ids=(subagent_id,),
            conversation_id=self._conversation_id_for_agent(subagent_id),
            status=candidate.status,
        )
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
        canonical_task_name: str = "",
        agent_path_segment: str = "",
    ) -> None:
        candidate_agent_path = self.validate_subagent_task_registration(
            subagent_id,
            parent_run_id=parent_run_id,
            canonical_task_name=canonical_task_name,
            agent_path_segment=agent_path_segment,
        )
        clean_subagent_id = str(subagent_id or "").strip()
        clean_parent_run_id = str(parent_run_id or "").strip()
        clean_task_name = str(canonical_task_name or "").strip()
        self._subagent_tasks[subagent_id] = task
        self._subagent_task_metadata[subagent_id] = {
            "subagent_id": subagent_id,
            "parent_run_id": clean_parent_run_id,
            "task_id": str(owner_task_id or "").strip(),
            "task_name": clean_task_name,
            "agent_path": candidate_agent_path,
            "session_id": str(session_id or "").strip(),
            "agent_type": str(agent_type or "").strip(),
            "prompt_summary": str(prompt_summary or "").strip(),
            "objective": str(prompt_summary or "").strip(),
            "background": bool(background),
            "status": "pending" if pending else "running",
        }
        if clean_task_name:
            self._register_subagent_names(
                clean_subagent_id,
                task_name=clean_task_name,
            )
        if subagent_id not in self._subagent_completion_events:
            self._subagent_completion_events[subagent_id] = asyncio.Event()
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
        self._record_agent_activity(
            "queued" if pending else "task_registered",
            agent_ids=(clean_subagent_id,),
            conversation_id=self._conversation_id_for_agent(clean_parent_run_id),
            status="pending" if pending else "running",
        )

    def validate_subagent_task_registration(
        self,
        subagent_id: str,
        *,
        parent_run_id: str = "",
        canonical_task_name: str = "",
        agent_path_segment: str = "",
    ) -> str:
        """Return the canonical child path or reject an occupied registration.

        Batch spawn callers use this before creating any worker. Registration
        calls the same check again as the final ownership fence, so validation
        and publication cannot drift into different path semantics.
        """

        clean_subagent_id = str(subagent_id or "").strip()
        if not clean_subagent_id:
            raise ValueError("subagent_id is required")
        clean_parent_run_id = str(parent_run_id or "").strip()
        clean_task_name = str(canonical_task_name or "").strip()
        existing = self._subagents.get(clean_subagent_id)
        candidate_agent_path = (
            str(existing.agent_path or "")
            if existing is not None and existing.agent_path
            else self._parent_agent_path(clean_parent_run_id)
            .child(agent_path_segment or clean_task_name or clean_subagent_id)
            .value
        )
        path_owner = self._agent_path_owner(
            candidate_agent_path,
            excluding_subagent_id=clean_subagent_id,
        )
        if path_owner:
            raise RuntimeError(
                f"Agent path {candidate_agent_path!r} is already owned by {path_owner}."
            )
        return candidate_agent_path

    def mark_subagent_task_running(self, subagent_id: str) -> None:
        metadata = self._subagent_task_metadata.get(str(subagent_id or "").strip())
        if isinstance(metadata, dict):
            metadata["status"] = "running"
            self._record_agent_activity(
                "running",
                agent_ids=(subagent_id,),
                conversation_id=self._conversation_id_for_agent(subagent_id),
                status="running",
            )

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
        if subagent_id not in self._subagents:
            self._subagent_name_registry = {
                name: registered_id
                for name, registered_id in self._subagent_name_registry.items()
                if registered_id != subagent_id
            }
        return True

    def get_subagent_task_metadata(self, subagent_id: str) -> dict[str, Any] | None:
        metadata = self._subagent_task_metadata.get(str(subagent_id or "").strip())
        return dict(metadata) if isinstance(metadata, dict) else None

    def _conversation_id_for_agent(self, agent_id: str) -> str:
        """Resolve conversation ownership through run/subagent parent edges."""

        current = str(agent_id or "").strip()
        visited: set[str] = set()
        while current and current not in visited and len(visited) < 128:
            visited.add(current)
            run = self._runs.get(current)
            if run is not None:
                return str(run.conversation_id or "").strip()
            subagent = self._subagents.get(current)
            if subagent is not None:
                current = str(subagent.parent_run_id or "").strip()
                continue
            metadata = self._subagent_task_metadata.get(current)
            if isinstance(metadata, dict):
                current = str(metadata.get("parent_run_id") or "").strip()
                continue
            break
        return ""

    def _record_agent_activity(
        self,
        kind: str,
        *,
        agent_ids: list[str] | tuple[str, ...] = (),
        conversation_id: str = "",
        status: str = "",
    ) -> int:
        clean_ids = tuple(
            dict.fromkeys(
                str(value or "").strip()
                for value in agent_ids
                if str(value or "").strip()
            )
        )
        owner = str(conversation_id or "").strip()
        if not owner:
            for candidate in clean_ids:
                owner = self._conversation_id_for_agent(candidate)
                if owner:
                    break
        with self._agent_activity_lock:
            self._agent_activity_seq += 1
            sequence = self._agent_activity_seq
            self._agent_activity_log.append(
                {
                    "seq": sequence,
                    "kind": str(kind or "activity").strip() or "activity",
                    "agent_ids": clean_ids,
                    "conversation_id": owner,
                    "status": str(status or "").strip(),
                    "timestamp": epoch_ms(),
                }
            )
            waiters = tuple(self._agent_activity_waiters)
        for loop, event in waiters:
            if loop.is_closed():
                continue
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                continue
        return sequence

    def agent_activity_cursor(self) -> int:
        with self._agent_activity_lock:
            return int(self._agent_activity_seq)

    def _matching_agent_activity_locked(
        self,
        *,
        after_seq: int,
        agent_ids: frozenset[str],
        conversation_id: str,
        kinds: frozenset[str],
    ) -> dict[str, Any] | None:
        for item in self._agent_activity_log:
            if int(item.get("seq") or 0) <= after_seq:
                continue
            if conversation_id and str(item.get("conversation_id") or "") != conversation_id:
                continue
            if kinds and str(item.get("kind") or "") not in kinds:
                continue
            item_ids = list(
                dict.fromkeys(
                    str(value or "").strip()
                    for value in item.get("agent_ids", ())
                    if str(value or "").strip()
                )
            )
            if agent_ids and agent_ids.isdisjoint(item_ids):
                continue
            return {
                **item,
                # Preserve the producer's stable sender/recipient order.  A
                # set is useful for matching but is not a public projection:
                # converting through one made otherwise identical wait
                # responses nondeterministic across processes.
                "agent_ids": item_ids,
            }
        return None

    async def wait_for_agent_activity(
        self,
        agent_ids: list[str],
        *,
        conversation_id: str = "",
        after_seq: int | None = None,
        timeout: float,
        kinds: Iterable[str] | None = None,
    ) -> dict[str, Any] | None:
        """Wait for bounded lifecycle or mailbox metadata without polling."""

        clean_ids = frozenset(
            str(value or "").strip()
            for value in agent_ids
            if str(value or "").strip()
        )
        owner = str(conversation_id or "").strip()
        allowed_kinds = frozenset(
            str(value or "").strip()
            for value in (kinds or ())
            if str(value or "").strip()
        )
        cursor = max(0, int(after_seq if after_seq is not None else self.agent_activity_cursor()))
        bounded_timeout = max(0.0, float(timeout))
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        waiter = (loop, event)
        with self._agent_activity_lock:
            existing = self._matching_agent_activity_locked(
                after_seq=cursor,
                agent_ids=clean_ids,
                conversation_id=owner,
                kinds=allowed_kinds,
            )
            if existing is not None or bounded_timeout <= 0:
                return existing
            self._agent_activity_waiters.add(waiter)

        deadline = loop.time() + bounded_timeout
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return None
                try:
                    await asyncio.wait_for(event.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    return None
                event.clear()
                with self._agent_activity_lock:
                    activity = self._matching_agent_activity_locked(
                        after_seq=cursor,
                        agent_ids=clean_ids,
                        conversation_id=owner,
                        kinds=allowed_kinds,
                    )
                if activity is not None:
                    return activity
        finally:
            with self._agent_activity_lock:
                self._agent_activity_waiters.discard(waiter)

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
        waiting for the slowest child or the full timeout. Mailbox-style wakeups
        let the coordinator react, steer, or
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

    def cancel_subagent_task(
        self,
        subagent_id: str,
        *,
        reason: str = "cancelled",
    ) -> Literal["cancelled", "done", "not_found"]:
        task = self._subagent_tasks.get(subagent_id)
        if task is None:
            return "not_found"
        metadata = self._subagent_task_metadata.get(subagent_id)
        if isinstance(metadata, dict):
            metadata["cancel_reason"] = str(reason or "cancelled")[:128]
        cancel_event = self._subagent_cancel_events.get(subagent_id)
        if cancel_event is not None:
            cancel_event.set()
        if task.done():
            self.release_subagent_task(subagent_id, expected_task=task)
            return "done"
        task.cancel()
        self.write_metric("subagent_task_cancel_requested", {"subagent_id": subagent_id})
        self._record_agent_activity(
            "cancel_requested",
            agent_ids=(subagent_id,),
            conversation_id=self._conversation_id_for_agent(subagent_id),
            status="cancelled",
        )
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
            status = self.cancel_subagent_task(subagent_id, reason=reason)
            if status in {"cancelled", "done"}:
                cancelled.append(subagent_id)
        if cancelled:
            self.write_metric(
                "subagent_children_cancel_requested",
                {"parent_run_id": parent, "subagent_ids": cancelled, "reason": reason},
            )
        return cancelled

    def register_subagent_cleanup_resource(
        self,
        subagent_id: str,
        *,
        resource_kind: str,
        resource_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Durably attach an owned resource before the child can use it."""

        clean_id = str(subagent_id or "").strip()
        kind = str(resource_kind or "").strip()
        owned_id = str(resource_id or "").strip()
        record = self._subagents.get(clean_id)
        if record is None or not kind or not owned_id or not self._owns_record(record):
            return False
        resources = [dict(item) for item in record.cleanup_resources]
        registered_at = epoch_ms()
        resource = {
            "resource_kind": kind,
            "resource_id": owned_id,
            "state": "active",
            "registered_at": registered_at,
            "metadata": dict(metadata or {}),
        }
        resources = [
            item
            for item in resources
            if not (
                str(item.get("resource_kind") or "") == kind
                and str(item.get("resource_id") or "") == owned_id
            )
        ]
        resources.append(resource)
        candidate = replace(record, cleanup_resources=resources)
        if self._swarm_store.upsert_subagent(
            candidate.to_dict(), expected_owner_token=self._runtime_owner_token
        ) is None:
            self._lease_lost = True
            return False
        self._subagents[clean_id] = candidate
        self.execution_journal(clean_id).append_cleanup(
            {
                **resource,
                "registered": True,
                "completed": False,
                "pending": False,
            }
        )
        return True

    def settle_subagent_cleanup_resource(
        self,
        subagent_id: str,
        *,
        resource_kind: str,
        resource_id: str,
        state: str,
        receipt: str,
    ) -> bool:
        """Persist a terminal cleanup receipt for one exact owned resource."""

        clean_id = str(subagent_id or "").strip()
        record = self._subagents.get(clean_id)
        if record is None or not self._owns_record(record):
            return False
        resources: list[dict[str, Any]] = []
        matched = False
        settled_at = epoch_ms()
        for raw in record.cleanup_resources:
            item = dict(raw)
            if (
                str(item.get("resource_kind") or "") == str(resource_kind or "")
                and str(item.get("resource_id") or "") == str(resource_id or "")
            ):
                item.update(
                    {
                        "state": str(state or "released"),
                        "receipt": str(receipt or "reconciled"),
                        "settled_at": settled_at,
                    }
                )
                matched = True
            resources.append(item)
        if not matched:
            return False
        candidate = replace(record, cleanup_resources=resources)
        if self._swarm_store.upsert_subagent(
            candidate.to_dict(), expected_owner_token=self._runtime_owner_token
        ) is None:
            self._lease_lost = True
            return False
        self._subagents[clean_id] = candidate
        self.execution_journal(clean_id).append_cleanup(
            {
                "resource_kind": str(resource_kind or ""),
                "resource_id": str(resource_id or ""),
                "state": str(state or "released"),
                "receipt": str(receipt or "reconciled"),
                "completed": True,
                "pending": False,
                "completed_at": settled_at,
            }
        )
        return True

    def _reconcile_subagent_worktrees(self, record: SubagentRunRecord) -> bool:
        record = self._subagents.get(record.subagent_id) or record
        completed = True
        for resource in record.cleanup_resources:
            if str(resource.get("state") or "active") != "active":
                continue
            if str(resource.get("resource_kind") or "") != "worktree":
                completed = False
                continue
            path_text = str(resource.get("resource_id") or "").strip()
            metadata = resource.get("metadata") if isinstance(resource.get("metadata"), dict) else {}
            git_root = str(metadata.get("git_root") or "").strip()
            if not path_text or not git_root:
                completed = False
                continue
            path = Path(path_text)
            if not path.exists():
                completed = self.settle_subagent_cleanup_resource(
                    record.subagent_id,
                    resource_kind="worktree",
                    resource_id=path_text,
                    state="released",
                    receipt="already_absent",
                ) and completed
                continue
            try:
                from backend.agent.worktree import (
                    cleanup_agent_worktree,
                    has_worktree_changes,
                    resume_agent_worktree,
                )

                worktree = resume_agent_worktree(
                    path,
                    expected_repo_root=git_root,
                    expected_subagent_id=record.subagent_id,
                )
                if worktree is None:
                    completed = False
                    continue
                had_changes = has_worktree_changes(worktree)
                kept, _kept_path = cleanup_agent_worktree(worktree)
                if kept and not had_changes:
                    completed = False
                    continue
                completed = self.settle_subagent_cleanup_resource(
                    record.subagent_id,
                    resource_kind="worktree",
                    resource_id=path_text,
                    state="retained" if kept else "released",
                    receipt="retained_user_changes" if kept else "removed_clean_worktree",
                ) and completed
            except Exception:
                logger.warning(
                    "Failed reconciling worktree for subagent %s",
                    record.subagent_id,
                    exc_info=True,
                )
                completed = False
        return completed

    def _reconcile_recovered_cleanup_resources(self) -> None:
        """Reconcile dead-runtime resources before clearing durable intent."""

        from backend.terminal.task_persistence import reconcile_owned_tasks

        for original in list(self._subagents.values()):
            if not original.cleanup_pending:
                continue
            worktrees_completed = self._reconcile_subagent_worktrees(original)
            record = self._subagents.get(original.subagent_id) or original
            task_report = (
                reconcile_owned_tasks(
                    record.session_id,
                    owner_task_ids={record.subagent_id},
                    parent_run_ids={record.subagent_id},
                    base_dir=self._runtime_state_root,
                )
                if record.session_id
                else None
            )
            # A legacy subagent with no session and no registered external
            # resources has nothing durable left to reconcile.  Once a task
            # or resource receipt exists, missing session identity is
            # uncertain and must remain pending.
            tasks_completed = (
                task_report.completed
                if task_report is not None
                else not bool(record.cleanup_resources)
            )
            if worktrees_completed and tasks_completed:
                self._mark_subagent_cleanup_complete(record.subagent_id)
            else:
                self.write_metric(
                    "subagent_cleanup_reconcile_pending",
                    {
                        "subagent_id": record.subagent_id,
                        "worktrees_completed": worktrees_completed,
                        "pending_task_ids": list(task_report.pending_task_ids) if task_report else [],
                        "errors": list(task_report.errors) if task_report else [],
                    },
                )

        for record in list(self._runs.values()):
            if not record.cleanup_pending:
                continue
            children_completed = all(
                not child.cleanup_pending
                for child in self._subagents.values()
                if child.parent_run_id == record.run_id
            )
            task_report = (
                reconcile_owned_tasks(
                    record.session_id,
                    owner_task_ids={record.run_id, record.task_id},
                    parent_run_ids={record.run_id},
                    base_dir=self._runtime_state_root,
                )
                if record.session_id
                else None
            )
            if children_completed and task_report is not None and task_report.completed:
                self._mark_run_cleanup_complete(record.run_id)
            else:
                # A run with no session identity yields no task report, so its
                # cleanup stays pending forever. Record why instead of leaving an
                # unexplained pending run behind.
                self.write_metric(
                    "run_cleanup_reconcile_pending",
                    {
                        "run_id": record.run_id,
                        "children_completed": children_completed,
                        "session_id_present": bool(record.session_id),
                        "pending_task_ids": list(task_report.pending_task_ids) if task_report else [],
                        "errors": list(task_report.errors) if task_report else [],
                    },
                )

    def _reconcile_terminal_subagent_cleanup(self, subagent_id: str) -> bool:
        """Settle every owned resource before clearing durable cleanup intent."""

        from backend.terminal.task_persistence import reconcile_owned_tasks

        clean_id = str(subagent_id or "").strip()
        record = self._subagents.get(clean_id)
        if record is None or not record.cleanup_pending:
            return record is not None
        worktrees_completed = self._reconcile_subagent_worktrees(record)
        record = self._subagents.get(clean_id) or record
        task_report = (
            reconcile_owned_tasks(
                record.session_id,
                owner_task_ids={record.subagent_id},
                parent_run_ids={record.subagent_id},
                base_dir=self._runtime_state_root,
                owner_terminal=True,
            )
            if record.session_id
            else None
        )
        tasks_completed = (
            task_report.completed
            if task_report is not None
            else not bool(record.cleanup_resources)
        )
        if worktrees_completed and tasks_completed:
            return self._mark_subagent_cleanup_complete(clean_id) is not None
        self.write_metric(
            "subagent_cleanup_reconcile_pending",
            {
                "subagent_id": clean_id,
                "worktrees_completed": worktrees_completed,
                "session_id_present": bool(record.session_id),
                "pending_task_ids": list(task_report.pending_task_ids) if task_report else [],
                "errors": list(task_report.errors)
                if task_report
                else (["missing_session_id"] if record.cleanup_resources else []),
            },
        )
        return False

    def _mark_run_cleanup_complete(self, run_id: str) -> AgentRunRecord | None:
        record = self._runs.get(str(run_id or "").strip())
        if record is None or not record.cleanup_pending:
            return record
        candidate = replace(record, cleanup_pending=False, cleanup_completed_at=epoch_ms())
        if self._swarm_store.upsert_agent_run(
            candidate.to_dict(), expected_owner_token=self._runtime_owner_token
        ) is None:
            self._lease_lost = True
            return None
        self._runs[run_id] = candidate
        self.write_metric("run_cleanup_completed", {"run_id": run_id})
        return candidate

    def _mark_subagent_cleanup(self, subagent_id: str, *, reason: str) -> SubagentRunRecord | None:
        """Persist cancellation intent before a bounded cleanup attempt."""
        record = self._subagents.get(str(subagent_id or "").strip())
        if record is None:
            return None
        now = epoch_ms()
        candidate = replace(
            record,
            cleanup_pending=True,
            cleanup_reason=str(reason or "cancelled"),
            cleanup_requested_at=now,
            cleanup_completed_at=None,
        )
        if self._swarm_store.upsert_subagent(
            candidate.to_dict(), expected_owner_token=self._runtime_owner_token
        ) is None:
            self._lease_lost = True
            return None
        self._subagents[subagent_id] = candidate
        self.write_metric(
            "subagent_cleanup_requested",
            {"subagent_id": subagent_id, "reason": reason},
        )
        self.execution_journal(subagent_id).append_cleanup(
            {
                "resource_kind": "subagent",
                "resource_id": subagent_id,
                "reason": str(reason or "cancelled"),
                "requested": True,
                "completed": False,
                "pending": True,
                "requested_at": now,
            }
        )
        return candidate

    def _mark_subagent_cleanup_complete(self, subagent_id: str) -> SubagentRunRecord | None:
        record = self._subagents.get(str(subagent_id or "").strip())
        if record is None or not record.cleanup_pending:
            return record
        candidate = replace(
            record,
            cleanup_pending=False,
            cleanup_completed_at=epoch_ms(),
        )
        if self._swarm_store.upsert_subagent(
            candidate.to_dict(), expected_owner_token=self._runtime_owner_token
        ) is None:
            self._lease_lost = True
            return None
        self._subagents[subagent_id] = candidate
        self.write_metric("subagent_cleanup_completed", {"subagent_id": subagent_id})
        self.execution_journal(subagent_id).append_cleanup(
            {
                "resource_kind": "subagent",
                "resource_id": subagent_id,
                "reason": candidate.cleanup_reason,
                "requested": True,
                "completed": True,
                "pending": False,
                "requested_at": candidate.cleanup_requested_at,
                "completed_at": candidate.cleanup_completed_at,
            }
        )
        return candidate

    def _retain_subagent_cleanup_owner(
        self,
        subagent_id: str,
        task: asyncio.Task[Any],
    ) -> None:
        """Reconcile resources after the owned task exits, then clear intent."""
        def settled(_completed: asyncio.Task[Any]) -> None:
            try:
                self._reconcile_terminal_subagent_cleanup(subagent_id)
            except Exception:
                logger.warning(
                    "Failed to persist completed cleanup for subagent %s",
                    subagent_id,
                    exc_info=True,
                )

        task.add_done_callback(settled)

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
            # Ordinary parent/task cancellation must preserve explicitly
            # detached children. Session shutdown and conversation deletion use
            # their dedicated force-cleanup paths instead.
            for subagent_id in self.cancel_child_subagent_tasks(parent_run_id, reason=reason, force=False):
                if subagent_id in seen:
                    continue
                seen.add(subagent_id)
                cancelled.append(subagent_id)
        # Compatibility/embedding fallback: a child created without a run id
        # is still owned by the user-visible task that launched it.
        for subagent_id, owner_task_id in list(self._subagent_owner_task_ids.items()):
            if owner_task_id != task or subagent_id in seen:
                continue
            record = self._subagents.get(subagent_id)
            if record is not None and (
                bool(getattr(record, "detach_from_parent", False))
                or not bool(getattr(record, "cancel_with_parent", True))
            ):
                continue
            status = self.cancel_subagent_task(subagent_id, reason=reason)
            if status in {"cancelled", "done"}:
                seen.add(subagent_id)
                cancelled.append(subagent_id)
        return cancelled

    async def stop_subagent_tasks_for_task(
        self,
        task_id: str,
        *,
        reason: str = "parent_cancelled",
    ) -> bool:
        """Request child cancellation and retain ownership until they drain."""

        subagent_ids = self.cancel_child_subagent_tasks_for_task(
            task_id,
            reason=reason,
        )
        for subagent_id in subagent_ids:
            self._mark_subagent_cleanup(subagent_id, reason=reason)
        owned = [
            (subagent_id, task)
            for subagent_id in subagent_ids
            if (task := self._subagent_tasks.get(subagent_id)) is not None
            and not task.done()
        ]
        initially_timed_out = await cancel_and_drain_to_completion(
            (task for _subagent_id, task in owned),
            timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
            label=f"task {task_id} subagents",
        )
        if initially_timed_out:
            self.write_metric(
                "subagent_task_cancel_timeout",
                {
                    "task_id": str(task_id or ""),
                    "subagent_ids": [
                        subagent_id
                        for subagent_id, task in owned
                        if task in initially_timed_out
                    ],
                    "reason": reason,
                    "drained_to_completion": True,
                },
            )
            for subagent_id, task in owned:
                if task in initially_timed_out:
                    self._retain_subagent_cleanup_owner(subagent_id, task)
        for subagent_id, task in owned:
            if task not in initially_timed_out and task.done():
                self._reconcile_terminal_subagent_cleanup(subagent_id)
        return True


    async def stop_subagent_tasks_for_session(
        self,
        session_id: str,
        *,
        reason: str = "session_shutdown",
    ) -> bool:
        """Cancel and drain every child task owned by one client session."""

        session = str(session_id or "").strip()
        if not session:
            return True
        owned = [
            (subagent_id, task)
            for subagent_id, task in self._subagent_tasks.items()
            if self._subagent_session_ids.get(subagent_id) == session
            and not task.done()
        ]
        for subagent_id, _task in owned:
            self.cancel_subagent_task(subagent_id, reason=reason)
            self._mark_subagent_cleanup(subagent_id, reason=reason)
        initially_timed_out = await cancel_and_drain_to_completion(
            (task for _subagent_id, task in owned),
            timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
            label=f"session {session} subagents",
        )
        if initially_timed_out:
            self.write_metric(
                "subagent_session_cancel_timeout",
                {
                    "session_id": session,
                    "subagent_ids": [
                        subagent_id
                        for subagent_id, task in owned
                        if task in initially_timed_out
                    ],
                    "reason": reason,
                    "drained_to_completion": True,
                },
            )
            for subagent_id, task in owned:
                if task in initially_timed_out:
                    self._retain_subagent_cleanup_owner(subagent_id, task)
        for subagent_id, task in owned:
            if task not in initially_timed_out and task.done():
                self._reconcile_terminal_subagent_cleanup(subagent_id)
        return True

    async def stop_subagent_tasks_for_conversation(
        self,
        conversation_id: str,
        *,
        reason: str = "conversation_deleted",
    ) -> bool:
        """Cancel and drain every child runtime owned by one conversation."""
        owner = str(conversation_id or "").strip()
        if not owner:
            return True
        parent_run_ids = {
            run_id
            for run_id, record in self._runs.items()
            if str(record.conversation_id or "").strip() == owner
        }
        durable_ids = self._swarm_store.conversation_agent_ids(owner)
        parent_run_ids.update(str(value) for value in durable_ids.get("run_ids", []))
        owner_task_ids = {str(value) for value in durable_ids.get("task_ids", [])}
        owner_task_ids.update(
            task_id
            for task_id, record in self._swarm_tasks.items()
            if str(record.conversation_id or "").strip() == owner
        )
        subagent_ids = {
            subagent_id
            for subagent_id, record in self._subagents.items()
            if str(record.parent_run_id or "").strip() in parent_run_ids
        }
        subagent_ids.update(str(value) for value in durable_ids.get("subagent_ids", []))
        subagent_ids.update(
            subagent_id
            for subagent_id, metadata in self._subagent_task_metadata.items()
            if isinstance(metadata, dict)
            and (
                str(metadata.get("parent_run_id") or "").strip() in parent_run_ids
                or str(metadata.get("task_id") or "").strip() in owner_task_ids
            )
        )
        while True:
            descendants = {
                subagent_id
                for subagent_id, record in self._subagents.items()
                if str(record.parent_run_id or "").strip() in subagent_ids
            }
            descendants.update(
                subagent_id
                for subagent_id, metadata in self._subagent_task_metadata.items()
                if isinstance(metadata, dict)
                and str(metadata.get("parent_run_id") or "").strip() in subagent_ids
            )
            next_ids = subagent_ids | descendants
            if len(next_ids) == len(subagent_ids):
                break
            subagent_ids = next_ids
        owned_tasks: list[tuple[str, asyncio.Task[Any]]] = []
        for subagent_id in subagent_ids:
            task = self._subagent_tasks.get(subagent_id)
            if task is not None and not task.done():
                owned_tasks.append((subagent_id, task))
            self.cancel_subagent_task(subagent_id, reason=reason)
            self._mark_subagent_cleanup(subagent_id, reason=reason)
        initially_timed_out = await cancel_and_drain_to_completion(
            (task for _subagent_id, task in owned_tasks),
            timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
            label=f"conversation {owner} subagents",
        )
        if initially_timed_out:
            self.write_metric(
                "subagent_conversation_cancel_timeout",
                {
                    "conversation_id": owner,
                    "subagent_ids": [
                        subagent_id
                        for subagent_id, task in owned_tasks
                        if task in initially_timed_out
                    ],
                    "reason": reason,
                    "drained_to_completion": True,
                },
            )
            for subagent_id, task in owned_tasks:
                if task in initially_timed_out:
                    self._retain_subagent_cleanup_owner(subagent_id, task)
        for subagent_id, task in owned_tasks:
            if task not in initially_timed_out and task.done():
                self._reconcile_terminal_subagent_cleanup(subagent_id)
        return True

    def get_subagent(self, subagent_id: str) -> SubagentRunRecord | None:
        return self._subagents.get(subagent_id)

    def load_persisted_subagent(self, subagent_id: str) -> SubagentRunRecord | None:
        """Read one durable subagent record without publishing it as live."""

        clean_id = str(subagent_id or "").strip()
        if not clean_id:
            return None
        payload = self._swarm_store.get_subagent(clean_id)
        return _subagent_from_dict(payload) if isinstance(payload, dict) else None

    def update_subagent_resume_config(
        self,
        subagent_id: str,
        resume_config: dict[str, Any],
        *,
        agent_path: str,
        mailbox_epoch: int,
    ) -> SubagentRunRecord | None:
        """Persist canonical configuration required to resume this incarnation."""

        clean_id = str(subagent_id or "").strip()
        record = self._subagents.get(clean_id)
        if record is None or record.status != "running":
            return None
        if not self._accepts_subagent_update(
            record,
            agent_path=agent_path,
            mailbox_epoch=mailbox_epoch,
        ):
            return None
        if not self._owns_record(record) or not self._refresh_runtime_lease():
            return None
        candidate = replace(record, resume_config=dict(resume_config))
        persisted = self._swarm_store.upsert_subagent(
            candidate.to_dict(),
            expected_owner_token=self._runtime_owner_token,
        )
        if persisted is None:
            self._lease_lost = True
            return None
        self._subagents[clean_id] = candidate
        return candidate

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
        teammate_name: str = "",
        task_name: str = "",
    ) -> None:
        """Register human-facing teammate and task names for targeting."""
        for raw_name in (teammate_name, task_name):
            key = str(raw_name or "").strip().casefold()
            if key:
                self._subagent_name_registry[key] = subagent_id

    def resolve_subagent_name(self, name: str) -> str:
        """Return the subagent id for a name/label, or '' if unknown.

        Falls back to the latest-registered id for that label; a live subagent
        id passed as-is still resolves via ``get_subagent`` at the call site.
        """
        key = str(name or "").strip().casefold()
        return self._subagent_name_registry.get(key, "")

    def update_subagent_lifecycle(
        self,
        subagent_id: str,
        *,
        agent_path: str,
        mailbox_epoch: int,
        current_activity: str | None = None,
        permission_mode: str | None = None,
        awaiting_plan_approval: bool | None = None,
        active_plan_request_id: str | None = None,
        is_idle: bool | None = None,
    ) -> SubagentRunRecord | None:
        record = self._subagents.get(str(subagent_id or "").strip())
        if record is None or record.status != "running":
            return None
        if (
            str(record.agent_path or "") != str(agent_path or "")
            or int(record.mailbox_epoch or 0) != int(mailbox_epoch or 0)
        ):
            return None
        candidate = replace(record)
        if current_activity is not None:
            candidate.current_activity = str(current_activity or "")
        if permission_mode is not None:
            candidate.permission_mode = str(permission_mode or "confirm")
        if awaiting_plan_approval is not None:
            candidate.awaiting_plan_approval = bool(awaiting_plan_approval)
        if active_plan_request_id is not None:
            candidate.active_plan_request_id = str(active_plan_request_id or "")
        if is_idle is not None:
            candidate.is_idle = bool(is_idle)
        persisted = self._swarm_store.upsert_subagent(
            candidate.to_dict(),
            expected_owner_token=self._runtime_owner_token,
        )
        if persisted is None:
            return None
        self._subagents[subagent_id] = candidate
        activity_kind = (
            "plan_approval"
            if awaiting_plan_approval is not None
            else "idle" if is_idle is True
            else "active" if is_idle is False
            else "permission_changed" if permission_mode is not None
            else "progress"
        )
        self._record_agent_activity(
            activity_kind,
            agent_ids=(subagent_id,),
            conversation_id=self._conversation_id_for_agent(subagent_id),
            status=candidate.status,
        )
        return candidate

    def update_running_run_parent(
        self,
        run_id: str,
        *,
        parent_run_id: str,
    ) -> AgentRunRecord | None:
        """Move one live coordinator run to the current teammate owner."""

        clean_id = str(run_id or "").strip()
        record = self._runs.get(clean_id)
        if record is None or record.status != "running":
            return None
        if not self._owns_record(record) or not self._refresh_runtime_lease():
            return None
        candidate = replace(record, parent_run_id=str(parent_run_id or "").strip())
        if self._swarm_store.upsert_agent_run(
            candidate.to_dict(),
            expected_owner_token=self._runtime_owner_token,
        ) is None:
            self._lease_lost = True
            return None
        self._runs[clean_id] = candidate
        self._registry.discard(clean_id, kind="run")
        self._registry.register(candidate, kind="run")
        return candidate

    def add_swarm_team_member(
        self,
        *,
        conversation_id: str,
        team_name: str,
        member: dict[str, Any],
    ) -> SwarmTeamRecord | None:
        payload = self._swarm_store.add_team_member(
            conversation_id=conversation_id,
            team_name=team_name,
            member=member,
        )
        if payload is None:
            return None
        team = _swarm_team_from_dict(payload)
        self._swarm_teams[team.team_name] = team
        return team

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
        public_usage = project_public_usage(usage)
        input_tokens = int(public_usage.get("input_tokens") or 0)
        output_tokens = int(public_usage.get("output_tokens") or 0)
        # Provider usage contracts count reasoning/thinking inside output
        # tokens.  Keep the detail field, but never add it a second time.
        total_tokens = input_tokens + output_tokens
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
            usage=public_usage,
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
        self._record_agent_activity(
            "result",
            agent_ids=(subagent_id,),
            conversation_id=self._conversation_id_for_agent(subagent_id),
            status=status,
        )
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
            result_dict = result.public_dict()
        else:
            stored = self._swarm_store.get_subagent_result(subagent_id)
            if isinstance(stored, dict):
                result_dict = project_public_subagent_result(stored)
        if record is None and result_dict is None and task is None:
            return None
        task_metadata = self._subagent_task_metadata.get(subagent_id, {})
        payload: dict[str, Any] = (
            record.public_dict()
            if record is not None
            else project_public_subagent_run(
                {"subagent_id": subagent_id, **task_metadata}
            )
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
            result.public_dict()
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
        with self._execution_journal_lock:
            journal = self._execution_journals.get(agent_id)
            if journal is None:
                journal = ExecutionJournal(agent_id, base_dir=self._journal_root)
                self._execution_journals[agent_id] = journal
            return journal

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
        session_id = ""
        if parent_run_id:
            parent_run = self._runs.get(parent_run_id)
            if parent_run is not None:
                conversation_id = str(getattr(parent_run, "conversation_id", "") or "").strip()
                session_id = str(getattr(parent_run, "session_id", "") or "").strip()
        if not parent_run_id and not conversation_id:
            return None
        # Synchronous TaskTool results already return to the parent as tool_result.
        # Only background or detached completions need next-turn outbox injection.
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
            session_id=session_id,
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
        # The mailbox protocol has a stable virtual leader participant. Keep that
        # literal on disk: rewriting it to a transient parent run id
        # makes the leader poller and the UI lose child->leader messages.
        effective_recipient_id = str(recipient_id or "").strip()
        virtual_parent_run_id = ""
        if effective_recipient_id == "parent" and sender_record is not None:
            parent_owner_id = str(sender_record.parent_run_id or "").strip()
            if not parent_owner_id:
                raise ValueError(f"subagent {sender_id} has no parent run mailbox")
            visited: set[str] = set()
            parent_run = None
            while (
                parent_owner_id
                and parent_owner_id not in visited
                and len(visited) < 128
            ):
                visited.add(parent_owner_id)
                parent_run = self.get_run(parent_owner_id)
                if parent_run is not None:
                    virtual_parent_run_id = parent_owner_id
                    break
                parent_agent = self.get_subagent(parent_owner_id)
                if parent_agent is None:
                    break
                parent_owner_id = str(parent_agent.parent_run_id or "").strip()
            if parent_run is None:
                raise ValueError(f"subagent {sender_id} has no valid parent ownership")
            if conversation_id and str(parent_run.conversation_id or "") != str(conversation_id):
                raise ValueError("parent mailbox conversation ownership mismatch")
            if team_name:
                sender_team = str(getattr(sender_record, "team_name", "") or "")
                if sender_team and sender_team != str(team_name):
                    raise ValueError("parent mailbox team ownership mismatch")
        recipient_record = self.get_subagent(effective_recipient_id)
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
        if effective_recipient_id in {"all", "*"}:
            for participant_id, participant in self._subagents.items():
                parent = self._runs.get(str(participant.parent_run_id or "").strip())
                participant_conversation = str(getattr(parent, "conversation_id", "") or "")
                if conversation_id and participant_conversation != conversation_id:
                    continue
                broadcast_epochs[participant_id] = int(participant.mailbox_epoch or 0)
        record = _swarm_message_from_dict(
            self._swarm_store.append_message({
                "sender_id": sender_id,
                "recipient_id": effective_recipient_id,
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
        activity_ids = [sender_id]
        if effective_recipient_id == "parent" and virtual_parent_run_id:
            activity_ids.append(virtual_parent_run_id)
        elif effective_recipient_id not in {"", "all", "*"}:
            activity_ids.append(effective_recipient_id)
        self._record_agent_activity(
            "message",
            agent_ids=activity_ids,
            conversation_id=conversation_id,
        )
        return record

    def reserve_lifecycle_response(
        self,
        *,
        response_kind: str,
        participant_id: str,
        mailbox_epoch: int,
        request_id: str,
        target_id: str = "",
        expected_active_plan_request_id: str | None = None,
    ) -> str:
        """Reserve one lifecycle reply under the shared durable control plane."""

        record = self.get_subagent(participant_id)
        if (
            record is None
            or str(record.status or "") != "running"
            or int(record.mailbox_epoch or 0) != int(mailbox_epoch or 0)
        ):
            return ""
        return self._swarm_store.reserve_lifecycle_response(
            response_kind=response_kind,
            participant_id=participant_id,
            mailbox_epoch=mailbox_epoch,
            request_id=request_id,
            target_id=target_id,
            expected_active_plan_request_id=expected_active_plan_request_id,
        )

    def commit_lifecycle_response(self, **kwargs: Any) -> bool:
        return self._swarm_store.commit_lifecycle_response(**kwargs)

    def release_lifecycle_response(self, **kwargs: Any) -> bool:
        return self._swarm_store.release_lifecycle_response(**kwargs)

    def list_swarm_messages(
        self,
        *,
        participant_id: str = "",
        conversation_id: str = "",
        since_seq: int = 0,
        limit: int = 20,
        mailbox_epoch: int | None = None,
        message_kind: str = "",
        correlation_id: str = "",
    ) -> list[SwarmMessageRecord]:
        bounded_limit = max(1, min(int(limit or 20), 100))
        records = [
            _swarm_message_from_dict(item)
            for item in self._swarm_store.list_messages(
                participant_id=participant_id,
                conversation_id=conversation_id,
                since_seq=since_seq,
                limit=(
                    bounded_limit
                    if message_kind
                    else 100
                    if mailbox_epoch is not None and participant_id
                    else bounded_limit
                ),
                message_kind=message_kind,
                correlation_id=correlation_id,
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
        lease_ms: int = MAILBOX_MESSAGE_LEASE_MS,
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

    def renew_swarm_message_claims(
        self,
        claims: list[MailboxMessageClaim],
        *,
        lease_ms: int = MAILBOX_MESSAGE_LEASE_MS,
    ) -> int:
        if not self._refresh_runtime_lease():
            return 0
        return self._swarm_store.renew_message_claims(
            [claim.claim_ref() for claim in claims],
            claim_owner=self._runtime_owner_token,
            lease_ms=lease_ms,
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

    def get_swarm_task(
        self,
        task_id: str,
        *,
        conversation_id: str = "",
    ) -> SwarmTaskRecord | None:
        payload = self._swarm_store.get_task(
            task_id,
            conversation_id=conversation_id,
        )
        if payload is None:
            cached = self._swarm_tasks.get(task_id)
            if cached is None:
                return None
            owner = str(conversation_id or "").strip()
            if owner and str(cached.conversation_id or "").strip() != owner:
                return None
            return cached
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

    def update_swarm_task(
        self,
        task_id: str,
        patch: dict[str, Any],
        *,
        conversation_id: str = "",
    ) -> SwarmTaskRecord | None:
        payload = self._swarm_store.update_task(
            task_id,
            patch,
            conversation_id=conversation_id,
        )
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
        conversation_id: str = "",
    ) -> SwarmTaskRecord | None:
        payload = self._swarm_store.append_output(
            task_id,
            {"author_id": author_id, "content": content},
            conversation_id=conversation_id,
        )
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

    def purge_conversation(self, conversation_id: str) -> dict[str, Any]:
        """Forget all durable and process-local agent state for a deleted chat."""
        owner = str(conversation_id or "").strip()
        if not owner:
            return {}
        if not self._refresh_runtime_lease():
            raise RuntimeError("runtime lease was lost before conversation purge")

        memory_run_ids = {
            run_id
            for run_id, record in self._runs.items()
            if str(record.conversation_id or "").strip() == owner
        }
        memory_task_ids = {
            task_id
            for task_id, record in self._swarm_tasks.items()
            if str(record.conversation_id or "").strip() == owner
        }
        memory_subagent_ids = {
            subagent_id
            for subagent_id, record in self._subagents.items()
            if str(record.parent_run_id or "").strip() in memory_run_ids
            or str(getattr(record, "task_id", "") or "").strip() in memory_task_ids
        }
        memory_subagent_ids.update(
            subagent_id
            for subagent_id, metadata in self._subagent_task_metadata.items()
            if isinstance(metadata, dict)
            and (
                str(metadata.get("parent_run_id") or "").strip() in memory_run_ids
                or str(metadata.get("task_id") or "").strip() in memory_task_ids
            )
        )
        while True:
            descendants = {
                subagent_id
                for subagent_id, record in self._subagents.items()
                if str(record.parent_run_id or "").strip() in memory_subagent_ids
            }
            descendants.update(
                subagent_id
                for subagent_id, metadata in self._subagent_task_metadata.items()
                if isinstance(metadata, dict)
                and str(metadata.get("parent_run_id") or "").strip() in memory_subagent_ids
            )
            next_ids = memory_subagent_ids | descendants
            if len(next_ids) == len(memory_subagent_ids):
                break
            memory_subagent_ids = next_ids
        memory_message_ids = {
            message_id
            for message_id, record in self._swarm_messages.items()
            if str(record.conversation_id or "").strip() == owner
        }
        memory_team_ids = {
            str(record.team_id or "")
            for record in self._swarm_teams.values()
            if str(record.conversation_id or "").strip() == owner
        }

        removed = self._swarm_store.purge_conversation(
            owner,
            allowed_active_owner_tokens={self._runtime_owner_token},
        )
        run_ids = memory_run_ids | {str(value) for value in removed.get("run_ids", [])}
        subagent_ids = memory_subagent_ids | {str(value) for value in removed.get("subagent_ids", [])}
        task_ids = memory_task_ids | {str(value) for value in removed.get("task_ids", [])}
        message_ids = memory_message_ids | {str(value) for value in removed.get("message_ids", [])}
        team_ids = memory_team_ids | {str(value) for value in removed.get("team_ids", [])}
        removed.update({
            "run_ids": sorted(run_ids),
            "subagent_ids": sorted(subagent_ids),
            "task_ids": sorted(task_ids),
            "message_ids": sorted(message_ids),
            "team_ids": sorted(team_ids),
        })
        cleanup_errors: list[dict[str, str]] = []

        for run_id in run_ids:
            self._runs.pop(run_id, None)
            self._registry.discard(run_id, kind="run")
        for subagent_id in subagent_ids:
            self._subagents.pop(subagent_id, None)
            self._subagent_results.pop(subagent_id, None)
            self._subagent_tasks.pop(subagent_id, None)
            self._subagent_task_metadata.pop(subagent_id, None)
            self._subagent_cancel_events.pop(subagent_id, None)
            self._subagent_completion_events.pop(subagent_id, None)
            self._subagent_parent_run_ids.pop(subagent_id, None)
            self._subagent_owner_task_ids.pop(subagent_id, None)
            self._subagent_session_ids.pop(subagent_id, None)
            self._subagent_slot_reservations.discard(subagent_id)
            self._registry.discard(subagent_id, kind="subagent")
        self._subagent_name_registry = {
            name: subagent_id
            for name, subagent_id in self._subagent_name_registry.items()
            if subagent_id not in subagent_ids
        }
        for task_id in task_ids:
            self._swarm_tasks.pop(task_id, None)
        for message_id in message_ids:
            self._swarm_messages.pop(message_id, None)
        self._swarm_teams = {
            name: team
            for name, team in self._swarm_teams.items()
            if str(team.team_id or "") not in team_ids
        }

        for agent_id in sorted(run_ids | subagent_ids):
            with self._execution_journal_lock:
                self._execution_journals.pop(agent_id, None)
            try:
                delete_agent_journal(agent_id, base_dir=self._journal_root)
            except Exception as exc:
                cleanup_errors.append(
                    {
                        "resource": "agent_journal",
                        "resource_id": agent_id,
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                    }
                )
            try:
                ParentNotificationOutbox(
                    parent_run_id=agent_id,
                    base_dir=self._outbox_root,
                ).delete()
            except Exception as exc:
                cleanup_errors.append(
                    {
                        "resource": "parent_outbox",
                        "resource_id": agent_id,
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                    }
                )
        try:
            ParentNotificationOutbox(
                conversation_id=owner,
                base_dir=self._outbox_root,
            ).delete()
        except Exception as exc:
            cleanup_errors.append(
                {
                    "resource": "conversation_outbox",
                    "resource_id": owner,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                }
            )
        if cleanup_errors:
            removed["cleanup_errors"] = cleanup_errors
            removed["cleanup_pending"] = True
            self.write_metric(
                "conversation_runtime_purge_cleanup_failed",
                {"conversation_id": owner, "cleanup_errors": cleanup_errors},
            )
        self.write_metric(
            "conversation_runtime_purged",
            {
                "conversation_id": owner,
                "run_count": len(run_ids),
                "subagent_count": len(subagent_ids),
                "task_count": len(task_ids),
                "message_count": len(message_ids),
                "team_count": len(team_ids),
            },
        )
        return removed

    def batched_metrics(self) -> Any:
        """Collapse a burst of metric appends into one atomic append.

        Runtime metrics are observational rather than authoritative.  A normal
        lifecycle event appends immediately; reconciliation and stress paths
        may collect a bounded burst and publish it with one O_APPEND write.
        """

        from contextlib import contextmanager

        @contextmanager
        def _batch() -> Any:
            with self._metric_batch_guard:
                if self._metric_batch is None:
                    self._metric_batch = []
                self._metric_batch_depth += 1
            try:
                yield
            finally:
                with self._metric_batch_guard:
                    self._metric_batch_depth -= 1
                    pending = (
                        self._metric_batch if self._metric_batch_depth <= 0 else None
                    )
                    if pending is not None:
                        self._metric_batch = None
                if pending:
                    self._append_metrics(pending)

        return _batch()

    def _append_metrics(self, metrics: list[dict[str, Any]]) -> None:
        if not metrics:
            return
        try:
            encoded = (
                "".join(
                    json.dumps(metric, ensure_ascii=False, default=str) + "\n"
                    for metric in metrics
                )
            ).encode("utf-8")
            with _METRIC_APPEND_LOCK:
                self._metrics_file.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(
                    os.fspath(self._metrics_file),
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                    0o644,
                )
                try:
                    written = os.write(descriptor, encoded)
                    if written != len(encoded):
                        raise OSError(
                            f"short metrics append: wrote {written} of {len(encoded)} bytes"
                        )
                finally:
                    os.close(descriptor)
        except Exception as exc:
            # Metrics are observational and must not alter run authority, but
            # a failed append is still runtime evidence. Keep a bounded
            # process-local receipt so snapshots expose the degraded surface.
            for metric in metrics:
                self._metric_write_failures.append(
                    {
                        "event": str(metric.get("event") or "runtime_metric")[:128],
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                        "recorded_at": epoch_ms(),
                    }
                )
            logger.error(
                "Runtime metric append failed for %d metric(s): %s",
                len(metrics),
                exc,
                exc_info=True,
            )

    def write_metric(self, event: str, payload: dict[str, Any]) -> None:
        try:
            event_name = str(event or "").strip()[:128] or "runtime_metric"
            metric = {
                "ts": epoch_ms(),
                "event": event_name,
                **project_public_metric_payload(event_name, payload),
            }
        except Exception as exc:
            self._metric_write_failures.append(
                {
                    "event": str(event or "runtime_metric")[:128],
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                    "recorded_at": epoch_ms(),
                }
            )
            logger.error(
                "Runtime metric projection failed for %s: %s", event, exc, exc_info=True
            )
            return
        with self._metric_batch_guard:
            batch = self._metric_batch
            if batch is not None:
                batch.append(metric)
                return
        self._append_metrics([metric])

    def list_runs(self, *, conversation_id: str = "", include_subagents: bool = False) -> dict[str, Any]:
        """Return a lightweight runtime snapshot for UI/debug panels."""
        runs = [
            record.public_dict()
            for record in self._runs.values()
            if not conversation_id or record.conversation_id == conversation_id
        ]
        payload: dict[str, Any] = {"runs": runs}
        if self._metric_write_failures:
            payload["metric_persistence"] = {
                "status": "degraded",
                "failures": [dict(item) for item in self._metric_write_failures],
            }
        if include_subagents:
            parent_ids = {str(record.get("run_id") or "") for record in runs}
            if conversation_id:
                # Subagent records intentionally inherit conversation ownership
                # through their parent instead of duplicating conversation_id.
                # Resolve the full transitive tree; filtering only direct
                # children makes grandchildren disappear from lifecycle and UI
                # projections even though their canonical path is valid.
                visible_records: list[SubagentRunRecord] = []
                visible_owner_ids = set(parent_ids)
                remaining_records = list(self._subagents.values())
                while remaining_records:
                    next_remaining: list[SubagentRunRecord] = []
                    added = False
                    for record in remaining_records:
                        if record.parent_run_id in visible_owner_ids:
                            visible_records.append(record)
                            visible_owner_ids.add(record.subagent_id)
                            added = True
                        else:
                            next_remaining.append(record)
                    if not added:
                        break
                    remaining_records = next_remaining
            else:
                visible_records = list(self._subagents.values())
                visible_owner_ids = {
                    *parent_ids,
                    *(record.subagent_id for record in visible_records),
                }
            subagents = [
                {
                    **record.public_dict(),
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
                for record in visible_records
            ]
            known_ids = {str(item.get("subagent_id") or "") for item in subagents}
            subagents.extend(
                {
                    **project_public_subagent_run(metadata),
                    "background_task": "done" if task.done() else "queued",
                    "result_available": False,
                }
                for subagent_id, task in self._subagent_tasks.items()
                if subagent_id not in known_ids
                and isinstance((metadata := self._subagent_task_metadata.get(subagent_id)), dict)
                and (
                    not conversation_id
                    or str(metadata.get("parent_run_id") or "") in visible_owner_ids
                )
            )
            payload["subagents"] = subagents
            payload["swarm_messages"] = [
                record.public_dict()
                for record in self.list_swarm_messages(conversation_id=conversation_id, limit=20)
            ]
            payload["swarm_tasks"] = [
                record.public_dict()
                for record in self.list_swarm_tasks(conversation_id=conversation_id, limit=50)
            ]
            payload["swarm_teams"] = [
                record.public_dict()
                for record in self.list_swarm_teams(conversation_id=conversation_id, limit=50)
            ]
        return payload


_DEFAULT_RUNTIME_LOCK = threading.Lock()
_DEFAULT_RUNTIME: AgentRuntime | None = None


def default_runtime_if_initialized() -> AgentRuntime | None:
    """Return the live process runtime without triggering durable recovery.

    Cleanup and notification probes are frequently no-ops in a process that
    has not executed an agent turn yet.  Those paths must not synchronously
    hydrate the entire durable runtime store just to discover that there is no
    process-local work to stop.  Callers that need to create or execute agent
    work must continue to use :func:`default_runtime`.
    """

    with _DEFAULT_RUNTIME_LOCK:
        runtime = _DEFAULT_RUNTIME
        if runtime is None or runtime._lease_lost:
            return None
        return runtime


def purge_persisted_conversation_runtime(conversation_id: str) -> dict[str, Any]:
    """Purge a deleted conversation without hydrating the process runtime.

    A session with no agent turn has no process-local runtime tasks to update,
    yet hard deletion still has to remove durable run, swarm, journal and
    notification records.  Loading every historical run before that targeted
    delete made the UI wait on an unbounded SQLite/JSON hydration.  This path
    performs the indexed delete directly and refuses to cross a live runtime
    lease owned by another process.
    """

    owner = str(conversation_id or "").strip()
    if not owner:
        return {}
    if default_runtime_if_initialized() is not None:
        raise RuntimeError(
            "the live process runtime must own in-memory conversation cleanup"
        )

    store = FileSwarmStore(SWARM_DIR)
    removed = store.purge_conversation(
        owner,
        allowed_active_owner_tokens=set(),
    )
    journal_root = SWARM_DIR.parent / "sidechains"
    outbox_root = SWARM_DIR.parent / "parent_notifications"
    agent_ids = {
        str(value or "").strip()
        for key in ("run_ids", "subagent_ids")
        for value in removed.get(key, [])
        if str(value or "").strip()
    }
    for agent_id in sorted(agent_ids):
        delete_agent_journal(agent_id, base_dir=journal_root)
        ParentNotificationOutbox(
            parent_run_id=agent_id,
            base_dir=outbox_root,
        ).delete()
    ParentNotificationOutbox(
        conversation_id=owner,
        base_dir=outbox_root,
    ).delete()
    return removed


def default_runtime() -> AgentRuntime:
    """Return a live process runtime, rebuilding after an orderly shutdown.

    FastAPI lifespan can be entered more than once in one interpreter (tests,
    embedded desktop restart, hot reload). ``close(release_lease=True)`` fences
    the old object permanently, so handing it out again would make every new
    turn fail with ``lease was lost``.
    """

    global _DEFAULT_RUNTIME
    with _DEFAULT_RUNTIME_LOCK:
        if _DEFAULT_RUNTIME is None or _DEFAULT_RUNTIME._lease_lost:
            _DEFAULT_RUNTIME = AgentRuntime()
        return _DEFAULT_RUNTIME