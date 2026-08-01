"""
Background task persistence for cross-session recovery.

Saves running background commands below the configured runtime state root (or
``~/.claude`` for CLI compatibility) so they survive session restarts. On
startup, scans for orphaned tasks and:
  - If process still alive (PID valid): reconnect
  - If process dead: mark failed and emit notification

Minimal implementation — no full process restore, just state tracking.
"""

import json
import logging
import os
import psutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from backend.agent.checkpoint import validate_storage_id

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
    process_start_time: float | None = None
    owner_pid: int | None = None
    owner_start_time: float | None = None


def get_tasks_dir(session_id: str, base_dir: Path | None = None) -> Path:
    """Return the isolated background-task directory for a session."""
    clean_session_id = validate_storage_id(session_id, field_name="session_id")
    if base_dir is None:
        state_root = str(os.environ.get("MINICODE_STATE_ROOT") or "").strip()
        base_dir = (
            Path(state_root).expanduser().resolve() / "data" / "agent-runtime"
            if state_root
            else Path.home() / ".claude"
        )
    tasks_dir = base_dir / "background_tasks" / clean_session_id
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
    process_start_time: float | None = None,
    owner_pid: int | None = None,
    owner_start_time: float | None = None,
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
    )
    tasks_dir = get_tasks_dir(session_id, base_dir)
    task_path = tasks_dir / f"{clean_task_id}.json"
    # 原子写入：先写临时文件，再os.replace（同一目录避免EXDEV跨设备错误）
    tmp_path = task_path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(asdict(state), f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, task_path)
    logger.debug(f"Task persisted: {task_path}")
    return task_path


def load_task(session_id: str, task_id: str, base_dir: Path | None = None) -> PersistedTaskState | None:
    """Load task state from disk."""
    clean_task_id = validate_storage_id(task_id, field_name="task_id")
    tasks_dir = get_tasks_dir(session_id, base_dir)
    task_path = tasks_dir / f"{clean_task_id}.json"
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
    if task_path.exists():
        task_path.unlink()
        logger.debug(f"Task deleted: {task_path}")


def list_persisted_tasks(session_id: str, base_dir: Path | None = None) -> list[PersistedTaskState]:
    """List all persisted tasks for this session."""
    tasks_dir = get_tasks_dir(session_id, base_dir)
    tasks = []
    for task_file in tasks_dir.glob("*.json"):
        try:
            with open(task_file, encoding="utf-8") as f:
                data = json.load(f)
            tasks.append(PersistedTaskState(**data))
        except Exception as exc:
            logger.warning(f"Failed to load task {task_file}: {exc}")
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


def _terminate_owned_process(task: PersistedTaskState) -> None:
    if not process_identity_matches(task.pid, task.process_start_time):
        return
    try:
        process = psutil.Process(int(task.pid))
        descendants = process.children(recursive=True)
        for child in reversed(descendants):
            child.terminate()
        process.terminate()
        _, alive = psutil.wait_procs([*descendants, process], timeout=3)
        for candidate in alive:
            candidate.kill()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return


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
        child_alive = process_identity_matches(task.pid, task.process_start_time)
        orphaned_record = (
            task.status == "running"
            and (
                (legacy_record and not is_process_alive(task.pid))
                or (not legacy_record and not owner_alive)
                or (task.pid is not None and not child_alive)
            )
        )
        if orphaned_record:
            orphaned.append(task)
            if not legacy_record and child_alive:
                _terminate_owned_process(task)
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
                process_start_time=task.process_start_time,
                owner_pid=task.owner_pid,
                owner_start_time=task.owner_start_time,
            )
    return orphaned
