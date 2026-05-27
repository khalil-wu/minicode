"""
产品级任务快照模型

根据 newplan.md 第 8.2 节 RuntimeTaskSnapshot 定义
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


TaskStatus = Literal["pending", "running", "waiting_approval", "completed", "failed", "cancelled"]
TaskOrigin = Literal["main_session", "background_command", "subagent", "system"]


@dataclass
class RuntimeTaskSnapshot:
    """
    产品级任务快照，用于前端展示和状态跟踪

    根据 newplan.md 第 8.2 节定义
    """
    id: str
    kind: str
    task_type: str
    label: str
    origin: TaskOrigin
    status: TaskStatus

    # 关联信息
    session_id: str | None = None
    conversation_id: str | None = None

    # 时间信息
    created_at: str = field(default_factory=_utc_now_iso)
    updated_at: str = field(default_factory=_utc_now_iso)
    started_at: str | None = None
    completed_at: str | None = None

    # 进度信息
    progress_text: str | None = None
    progress_percent: float | None = None

    # 输出和结果
    latest_output_preview: str | None = None
    latest_artifact_id: str | None = None

    # 审批相关
    awaiting_approval: bool = False
    approval_id: str | None = None

    # 错误信息
    error: str | None = None

    # 控制能力
    can_stop: bool = True

    # 层级关系
    parent_task_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """转换为字典，用于 WebSocket 传输"""
        return {
            "id": self.id,
            "kind": self.kind,
            "task_type": self.task_type,
            "label": self.label,
            "origin": self.origin,
            "status": self.status,
            "session_id": self.session_id,
            "conversation_id": self.conversation_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "progress_text": self.progress_text,
            "progress_percent": self.progress_percent,
            "latest_output_preview": self.latest_output_preview,
            "latest_artifact_id": self.latest_artifact_id,
            "awaiting_approval": self.awaiting_approval,
            "approval_id": self.approval_id,
            "error": self.error,
            "can_stop": self.can_stop,
            "parent_task_id": self.parent_task_id,
        }

    @property
    def is_terminal(self) -> bool:
        """是否为终态"""
        return self.status in {"completed", "failed", "cancelled"}

    @property
    def is_active(self) -> bool:
        """是否为活跃状态"""
        return self.status in {"running", "waiting_approval"}

    @property
    def needs_attention(self) -> bool:
        """是否需要用户关注"""
        return self.status in {"waiting_approval", "failed"}

    def calculate_duration(self) -> float | None:
        """计算任务耗时（秒）"""
        if not self.started_at:
            return None

        end_time = self.completed_at or _utc_now_iso()
        try:
            start = datetime.fromisoformat(self.started_at)
            end = datetime.fromisoformat(end_time)
            return (end - start).total_seconds()
        except (ValueError, TypeError):
            return None


@dataclass
class TaskSummary:
    """任务摘要统计"""
    total: int = 0
    pending: int = 0
    running: int = 0
    waiting_approval: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "total": self.total,
            "pending": self.pending,
            "running": self.running,
            "waiting_approval": self.waiting_approval,
            "completed": self.completed,
            "failed": self.failed,
            "cancelled": self.cancelled,
        }

    @classmethod
    def from_tasks(cls, tasks: list[RuntimeTaskSnapshot]) -> TaskSummary:
        """从任务列表生成摘要"""
        summary = cls(total=len(tasks))
        for task in tasks:
            if task.status == "pending":
                summary.pending += 1
            elif task.status == "running":
                summary.running += 1
            elif task.status == "waiting_approval":
                summary.waiting_approval += 1
            elif task.status == "completed":
                summary.completed += 1
            elif task.status == "failed":
                summary.failed += 1
            elif task.status == "cancelled":
                summary.cancelled += 1
        return summary
