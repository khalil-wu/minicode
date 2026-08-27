"""
Background task persistence for cross-session recovery.

Saves running background commands below MiniCode's runtime state root so they
survive session restarts. On
startup, scans for orphaned tasks and:
  - If process still alive (PID valid): reconnect
  - If process dead: mark failed and emit notification

Minimal implementation — no full process restore, just state tracking.
"""

import json
import logging
import os
import psutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from backend.agent.checkpoint import validate_storage_id
from backend.atomic_io import atomic_write_text, file_mutation_locks
from backend.runtime_paths import agent_runtime_root

logger = logging.getLogger(__name__)


@dataclass
class PersistedTaskState:
    """Serializable background task state."""
    task_id: str
    command: str
    description: str
    cwd: str
    pid: int | None
    started_at: float
    timeout_ms: int
    status: str  # "running" | "completed" | "failed" | "interrupted"
    conversation_id: str = ""
    owner_task_id: str = ""
    parent_run_id: str = ""
    process_start_time: float | None = None
    owner_pid: int | None = None
    owner_start_time: float | None = None
    cleanup_pending: bool = False
    cleanup_reason: str = ""
    cleanup_requested_at: float | None = None
    cleanup_completed_at: float | None = None


@dataclass(frozen=True)
class OwnedTaskCleanupReport:
    """Evidence produced while reconciling one durable task owner."""

    matched_task_ids: tuple[str, ...] = ()
    completed_task_ids: tuple[str, ...] = ()
    pending_task_ids: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def completed(self) -> bool:
        return not self.pending_task_ids and not self.errors


def get_tasks_dir(session_id: str, base_dir: Path | None = None) -> Path:
    """Return the isolated background-task directory for a session."""
    clean_session_id = validate_storage_id(session_id, field_name="session_id")
    tasks_dir = agent_runtime_root(base_dir) / "background_tasks" / clean_session_id
    tasks_dir.mkdir(parents=True, exist_ok=True)
    return tasks_dir


def save_task(
    session_id: str,
    task_id: str,
    command: str,
    description: str,
    cwd: str,
    pid: int | None,
    started_at: float,
    timeout_ms: int,
    status: str = "running",
    base_dir: Path | None = None,
    conversation_id: str = "",
    owner_task_id: str = "",
    parent_run_id: str = "",
    process_start_time: float | None = None,
    owner_pid: int | None = None,
    owner_start_time: float | None = None,
    cleanup_pending: bool = False,
    cleanup_reason: str = "",
    cleanup_requested_at: float | None = None,
    cleanup_completed_at: float | None = None,
) -> Path:
    """Save task state to disk."""
    clean_task_id = validate_storage_id(task_id, field_name="task_id")
    resolved_owner_pid = os.getpid() if owner_pid is None else owner_pid
    state = PersistedTaskState(
        task_id=task_id,
        command=command,
        description=description,
        cwd=cwd,
        pid=pid,
        started_at=started_at,
        timeout_ms=timeout_ms,
        status=status,
        conversation_id=str(conversation_id or ""),
        owner_task_id=str(owner_task_id or ""),
        parent_run_id=str(parent_run_id or ""),
        process_start_time=(
            process_start_time
            if process_start_time is not None
            else get_process_start_time(pid)
        ),
        owner_pid=resolved_owner_pid,
        owner_start_time=(
            owner_start_time
            if owner_start_time is not None
            else get_process_start_time(resolved_owner_pid)
        ),
        cleanup_pending=bool(cleanup_pending),
        cleanup_reason=str(cleanup_reason or ""),
        cleanup_requested_at=cleanup_requested_at,
        cleanup_completed_at=cleanup_completed_at,
    )
    tasks_dir = get_tasks_dir(session_id, base_dir)
    task_path = tasks_dir / f"{clean_task_id}.json"
    with file_mutation_locks([task_path]):
        atomic_write_text(task_path, json.dumps(asdict(state), indent=2))
    logger.debug(f"Task persisted: {task_path}")
    return task_path


def load_task(session_id: str, task_id: str, base_dir: Path | None = None) -> PersistedTaskState | None:
    """Load task state from disk."""
    clean_task_id = validate_storage_id(task_id, field_name="task_id")
    tasks_dir = get_tasks_dir(session_id, base_dir)
    task_path = tasks_dir / f"{clean_task_id}.json"
    with file_mutation_locks([task_path]):
        if not task_path.exists():
            return None
        with open(task_path, encoding="utf-8") as f:
            data = json.load(f)
        return PersistedTaskState(**data)


def delete_task(session_id: str, task_id: str, base_dir: Path | None = None) -> None:
    """Delete task state file."""
    clean_task_id = validate_storage_id(task_id, field_name="task_id")
    tasks_dir = get_tasks_dir(session_id, base_dir)
    task_path = tasks_dir / f"{clean_task_id}.json"
    with file_mutation_locks([task_path]):
        if task_path.exists():
            task_path.unlink()
            logger.debug(f"Task deleted: {task_path}")


def list_persisted_tasks(session_id: str, base_dir: Path | None = None) -> list[PersistedTaskState]:
    """List all persisted tasks for this session."""
    tasks_dir = get_tasks_dir(session_id, base_dir)
    tasks = []
    for task_file in tasks_dir.glob("*.json"):
        try:
            with file_mutation_locks([task_file]):
                with open(task_file, encoding="utf-8") as f:
                    data = json.load(f)
            tasks.append(PersistedTaskState(**data))
        except Exception as exc:
            # A malformed durable owner record is not disposable metadata. It
            # may describe a live process whose identity cannot be verified;
            # abort reconciliation so the caller reports an uncertain cleanup
            # state instead of silently leaving an orphan behind.
            raise RuntimeError(
                f"Persisted background task record is unreadable: {task_file}"
            ) from exc
    return tasks


def is_process_alive(pid: int | None) -> bool:
    """Check if process is still running."""
    if pid is None:
        return False
    try:
        proc = psutil.Process(pid)
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False


def get_process_start_time(pid: int | None) -> float | None:
    if pid is None:
        return None
    try:
        return float(psutil.Process(pid).create_time())
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None


def process_identity_matches(pid: int | None, expected_start_time: float | None) -> bool:
    if pid is None or expected_start_time is None:
        return False
    actual = get_process_start_time(pid)
    return actual is not None and abs(actual - expected_start_time) < 0.01


def child_is_live(task: PersistedTaskState) -> bool:
    """Whether the recorded child still runs, tolerating legacy records.

    A record written before process fencing has no ``process_start_time``, so
    identity cannot be proven. Such a PID must still count as live: reporting it
    as gone would claim a cleanup that never happened.
    """
    if task.pid is None:
        return False
    if task.process_start_time is not None:
        return process_identity_matches(task.pid, task.process_start_time)
    return is_process_alive(task.pid)


def _terminate_owned_process(task: PersistedTaskState) -> bool:
    """Terminate the fenced process tree and report whether it fully exited."""
    if not process_identity_matches(task.pid, task.process_start_time):
        return True
    try:
        process = psutil.Process(int(task.pid))
        descendants = process.children(recursive=True)
        for child in reversed(descendants):
            child.terminate()
        process.terminate()
        _, alive = psutil.wait_procs([*descendants, process], timeout=3)
        for candidate in alive:
            candidate.kill()
        _, remaining = psutil.wait_procs(alive, timeout=1)
        return not remaining
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return not process_identity_matches(task.pid, task.process_start_time)


def cleanup_orphaned_tasks(session_id: str, base_dir: Path | None = None) -> list[PersistedTaskState]:
    """
    Scan persisted tasks and fence records whose owning MiniCode process ended.

    A surviving child PID is not attachable: the old process owned its pipes and
    completion callback. Keeping it as "running" makes the UI lie and can leave
    a command mutating the workspace without an owner. Such children are
    terminated only when their PID start identity still matches the persisted
    identity, then the record becomes ``interrupted``.
    """
    orphaned = []
    for task in list_persisted_tasks(session_id, base_dir):
        legacy_record = task.owner_pid is None or task.owner_start_time is None
        owner_alive = process_identity_matches(task.owner_pid, task.owner_start_time)
        child_alive = child_is_live(task)
        orphaned_record = (
            (task.status == "running" or task.cleanup_pending)
            and (
                # A legacy record carries no owner fence, so this process cannot
                # claim it; otherwise the owner or the child must be gone.
                legacy_record
                or not owner_alive
                or (task.pid is not None and not child_alive)
            )
        )
        if orphaned_record:
            cleanup_requested_at = time.time()
            cleanup_completed = True
            cleanup_reason = "background_owner_exited"
            if legacy_record and child_alive:
                # The old record has no owner/process start identity, so the
                # child cannot be killed safely without risking PID reuse. It
                # is nevertheless no longer an owned running task. Mark the
                # uncertainty durably instead of leaving it as `running`.
                cleanup_completed = False
                cleanup_reason = "legacy_process_identity_unavailable"
            elif not legacy_record and child_alive:
                cleanup_completed = _terminate_owned_process(task)
                if not cleanup_completed:
                    cleanup_reason = "owned_process_survived_reaper"
            save_task(
                session_id=session_id,
                task_id=task.task_id,
                command=task.command,
                description=task.description,
                cwd=task.cwd,
                pid=task.pid,
                started_at=task.started_at,
                timeout_ms=task.timeout_ms,
                status="interrupted",
                base_dir=base_dir,
                conversation_id=task.conversation_id,
                owner_task_id=task.owner_task_id,
                parent_run_id=task.parent_run_id,
                process_start_time=task.process_start_time,
                owner_pid=task.owner_pid,
                owner_start_time=task.owner_start_time,
                cleanup_pending=not cleanup_completed,
                cleanup_reason=cleanup_reason,
                cleanup_requested_at=cleanup_requested_at,
                cleanup_completed_at=(time.time() if cleanup_completed else None),
            )
            recovered = load_task(session_id, task.task_id, base_dir)
            if recovered is None:
                raise RuntimeError(
                    f"Recovered background task {task.task_id!r} disappeared after commit"
                )
            orphaned.append(recovered)
    return orphaned


def reconcile_owned_tasks(
    session_id: str,
    *,
    owner_task_ids: set[str] | None = None,
    parent_run_ids: set[str] | None = None,
    base_dir: Path | None = None,
    owner_terminal: bool = False,
) -> OwnedTaskCleanupReport:
    """Reap background processes durably attributed to a dead agent owner.

    A PID is actionable only when its persisted process start time still
    matches. Records owned by another live MiniCode process remain pending.
    Malformed records make the result uncertain instead of being skipped and
    accidentally producing a false cleanup completion.
    """

    clean_owner_ids = {str(value or "").strip() for value in owner_task_ids or set() if str(value or "").strip()}
    clean_parent_ids = {str(value or "").strip() for value in parent_run_ids or set() if str(value or "").strip()}
    if not clean_owner_ids and not clean_parent_ids:
        return OwnedTaskCleanupReport()

    tasks_dir = get_tasks_dir(session_id, base_dir)
    matched: list[str] = []
    completed: list[str] = []
    pending: list[str] = []
    errors: list[str] = []

    for task_file in sorted(tasks_dir.glob("*.json")):
        try:
            with file_mutation_locks([task_file]):
                data = json.loads(task_file.read_text(encoding="utf-8"))
            task = PersistedTaskState(**data)
        except Exception as exc:
            errors.append(f"{task_file.name}: {exc}")
            continue

        if task.owner_task_id not in clean_owner_ids and task.parent_run_id not in clean_parent_ids:
            continue
        matched.append(task.task_id)

        legacy_owner = task.owner_pid is None or task.owner_start_time is None
        owner_alive = process_identity_matches(task.owner_pid, task.owner_start_time)
        child_alive = child_is_live(task)
        if owner_alive and not owner_terminal:
            pending.append(task.task_id)
            continue
        if legacy_owner and child_alive:
            # A live legacy PID has no owner/process fencing strong enough to
            # authorize termination.
            pending.append(task.task_id)
            continue
        if child_alive and not process_identity_matches(task.pid, task.process_start_time):
            # The PID is live but its start-time identity was never recorded, so
            # killing it could hit an unrelated process after PID reuse. Report
            # the unfinished cleanup instead of claiming one.
            pending.append(task.task_id)
            continue

        cleanup_completed = _terminate_owned_process(task) if child_alive else True
        cleanup_reason = (
            "agent_owner_exited"
            if cleanup_completed
            else "owned_process_survived_reaper"
        )
        try:
            save_task(
                session_id=session_id,
                task_id=task.task_id,
                command=task.command,
                description=task.description,
                cwd=task.cwd,
                pid=task.pid,
                started_at=task.started_at,
                timeout_ms=task.timeout_ms,
                status="interrupted" if task.status == "running" else task.status,
                base_dir=base_dir,
                conversation_id=task.conversation_id,
                owner_task_id=task.owner_task_id,
                parent_run_id=task.parent_run_id,
                process_start_time=task.process_start_time,
                owner_pid=task.owner_pid,
                owner_start_time=task.owner_start_time,
                cleanup_pending=not cleanup_completed,
                cleanup_reason=cleanup_reason,
                cleanup_requested_at=task.cleanup_requested_at or time.time(),
                cleanup_completed_at=time.time() if cleanup_completed else None,
            )
        except Exception as exc:
            errors.append(f"{task.task_id}: {exc}")
            continue
        if cleanup_completed:
            completed.append(task.task_id)
        else:
            pending.append(task.task_id)

    return OwnedTaskCleanupReport(
        matched_task_ids=tuple(matched),
        completed_task_ids=tuple(completed),
        pending_task_ids=tuple(pending),
        errors=tuple(errors),
    )
