"""
Background task persistence for cross-session recovery.

Saves running background commands to `.claude/background_tasks/<session>/<task>.json`
so they survive session restarts. On startup, scans for orphaned tasks and:
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
    status: str  # "running" | "completed" | "failed"


def get_tasks_dir(session_id: str, base_dir: Path | None = None) -> Path:
    """Returns .claude/background_tasks/<session_id>/"""
    if base_dir is None:
        base_dir = Path.home() / ".claude"
    tasks_dir = base_dir / "background_tasks" / session_id
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
) -> Path:
    """Save task state to disk."""
    state = PersistedTaskState(
        task_id=task_id,
        command=command,
        description=description,
        cwd=cwd,
        pid=pid,
        started_at=started_at,
        timeout_ms=timeout_ms,
        status=status,
    )
    tasks_dir = get_tasks_dir(session_id, base_dir)
    task_path = tasks_dir / f"{task_id}.json"
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
    tasks_dir = get_tasks_dir(session_id, base_dir)
    task_path = tasks_dir / f"{task_id}.json"
    if not task_path.exists():
        return None
    with open(task_path, encoding="utf-8") as f:
        data = json.load(f)
    return PersistedTaskState(**data)


def delete_task(session_id: str, task_id: str, base_dir: Path | None = None) -> None:
    """Delete task state file."""
    tasks_dir = get_tasks_dir(session_id, base_dir)
    task_path = tasks_dir / f"{task_id}.json"
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
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def cleanup_orphaned_tasks(session_id: str, base_dir: Path | None = None) -> list[PersistedTaskState]:
    """
    Scan persisted tasks, return orphaned ones (process dead but state says running).
    Caller should mark them failed and emit notifications.
    """
    orphaned = []
    for task in list_persisted_tasks(session_id, base_dir):
        if task.status == "running" and not is_process_alive(task.pid):
            orphaned.append(task)
            # Update state to failed
            save_task(
                session_id=session_id,
                task_id=task.task_id,
                command=task.command,
                description=task.description,
                cwd=task.cwd,
                pid=task.pid,
                started_at=task.started_at,
                timeout_ms=task.timeout_ms,
                status="failed",
                base_dir=base_dir,
            )
    return orphaned
