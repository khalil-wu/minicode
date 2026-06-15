"""
产品级任务管理器

扩展基础 TaskManager，提供产品级任务跟踪和状态管理
根据 newplan.md Phase 2 要求实现
"""
from __future__ import annotations

from typing import Any, Callable

from backend.tasks.manager import TaskManager
from backend.tasks.models import RuntimeTaskSnapshot, TaskSummary, TaskOrigin, TaskStatus


class ProductTaskManager:
    """
    产品级任务管理器

    在基础 TaskManager 之上提供：
    - 产品级任务快照
    - 任务标签和描述
    - 进度跟踪
    - 审批状态
    - 任务分组和过滤
    """

    def __init__(
        self,
        base_manager: TaskManager | None = None,
        on_task_update: Callable[[RuntimeTaskSnapshot], None] | None = None,
    ):
        self._base_manager = base_manager or TaskManager()
        self._on_task_update = on_task_update
        self._task_metadata: dict[str, dict[str, Any]] = {}
        self._session_id: str | None = None
        self._conversation_id: str | None = None

    def set_session_context(self, session_id: str, conversation_id: str | None = None):
        """设置当前会话上下文"""
        self._session_id = session_id
        self._conversation_id = conversation_id

    def create_task(
        self,
        awaitable: Any,
        *,
        label: str,
        task_type: str = "agent",
        origin: TaskOrigin = "main_session",
        can_stop: bool = True,
        timeout: float | None = None,
    ) -> RuntimeTaskSnapshot:
        """
        创建产品级任务

        Args:
            awaitable: 异步任务
            label: 任务标签（用户可见）
            task_type: 任务类型
            origin: 任务来源
            can_stop: 是否可停止
            timeout: 超时时间
        """
        # 创建基础任务
        managed = self._base_manager.create(
            kind=task_type,
            awaitable=awaitable,
            timeout=timeout,
        )

        # 创建产品级快照
        snapshot = RuntimeTaskSnapshot(
            id=managed.id,
            kind=managed.kind,
            task_type=task_type,
            label=label,
            origin=origin,
            status="running",
            session_id=self._session_id,
            conversation_id=self._conversation_id,
            created_at=managed.created_at,
            updated_at=managed.updated_at,
            started_at=managed.created_at,
            can_stop=can_stop,
        )

        # 保存元数据
        self._task_metadata[managed.id] = {
            "label": label,
            "task_type": task_type,
            "origin": origin,
            "can_stop": can_stop,
        }

        # 通知更新
        self._notify_task_update(snapshot)

        return snapshot

    def update_task_progress(
        self,
        task_id: str,
        progress_text: str | None = None,
        progress_percent: float | None = None,
        output_preview: str | None = None,
    ) -> RuntimeTaskSnapshot | None:
        """更新任务进度"""
        snapshot = self.get_task_snapshot(task_id)
        if not snapshot:
            return None

        if progress_text is not None:
            snapshot.progress_text = progress_text
        if progress_percent is not None:
            snapshot.progress_percent = max(0.0, min(100.0, progress_percent))
        if output_preview is not None:
            snapshot.latest_output_preview = output_preview

        self._notify_task_update(snapshot)
        return snapshot

    def set_task_waiting_approval(
        self,
        task_id: str,
        approval_id: str,
    ) -> RuntimeTaskSnapshot | None:
        """设置任务等待审批"""
        snapshot = self.get_task_snapshot(task_id)
        if not snapshot:
            return None

        snapshot.status = "waiting_approval"
        snapshot.awaiting_approval = True
        snapshot.approval_id = approval_id

        self._notify_task_update(snapshot)
        return snapshot

    def resume_task_from_approval(
        self,
        task_id: str,
    ) -> RuntimeTaskSnapshot | None:
        """从审批恢复任务"""
        snapshot = self.get_task_snapshot(task_id)
        if not snapshot:
            return None

        snapshot.status = "running"
        snapshot.awaiting_approval = False
        snapshot.approval_id = None

        self._notify_task_update(snapshot)
        return snapshot

    def get_task_snapshot(self, task_id: str) -> RuntimeTaskSnapshot | None:
        """获取任务快照"""
        managed = self._base_manager.get(task_id)
        if not managed:
            return None

        metadata = self._task_metadata.get(task_id, {})

        # 从基础任务构建快照
        snapshot = RuntimeTaskSnapshot(
            id=managed.id,
            kind=managed.kind,
            task_type=metadata.get("task_type", managed.kind),
            label=metadata.get("label", f"Task {managed.id}"),
            origin=metadata.get("origin", "main_session"),
            status=self._map_status(managed.status),
            session_id=self._session_id,
            conversation_id=self._conversation_id,
            created_at=managed.created_at,
            updated_at=managed.updated_at,
            started_at=managed.created_at,
            completed_at=managed.updated_at if managed.is_terminal else None,
            error=managed.error,
            can_stop=metadata.get("can_stop", True) and not managed.is_terminal,
        )

        return snapshot

    def list_tasks(
        self,
        *,
        status_filter: TaskStatus | None = None,
        origin_filter: TaskOrigin | None = None,
        include_terminal: bool = True,
    ) -> list[RuntimeTaskSnapshot]:
        """列出任务"""
        snapshots = []
        for managed in self._base_manager.list():
            snapshot = self.get_task_snapshot(managed.id)
            if not snapshot:
                continue

            # 过滤终态任务
            if not include_terminal and snapshot.is_terminal:
                continue

            # 状态过滤
            if status_filter and snapshot.status != status_filter:
                continue

            # 来源过滤
            if origin_filter and snapshot.origin != origin_filter:
                continue

            snapshots.append(snapshot)

        # 按更新时间倒序
        snapshots.sort(key=lambda s: s.updated_at, reverse=True)
        return snapshots

    def get_task_summary(self) -> TaskSummary:
        """获取任务摘要"""
        tasks = self.list_tasks(include_terminal=True)
        return TaskSummary.from_tasks(tasks)

    def get_running_tasks(self) -> list[RuntimeTaskSnapshot]:
        """获取运行中的任务"""
        return [t for t in self.list_tasks() if t.status == "running"]

    def get_pending_approval_tasks(self) -> list[RuntimeTaskSnapshot]:
        """获取待审批的任务"""
        return [t for t in self.list_tasks() if t.awaiting_approval]

    def get_failed_tasks(self) -> list[RuntimeTaskSnapshot]:
        """获取失败的任务"""
        return [t for t in self.list_tasks() if t.status == "failed"]

    def cancel_task(self, task_id: str) -> RuntimeTaskSnapshot | None:
        """取消任务"""
        success = self._base_manager.cancel(task_id)
        if not success:
            return None

        snapshot = self.get_task_snapshot(task_id)
        if snapshot:
            snapshot.status = "cancelled"
            self._notify_task_update(snapshot)
        return snapshot

    def prune_terminal_tasks(self) -> int:
        """清理终态任务"""
        removed = self._base_manager.prune()
        # 清理元数据
        for task_id in list(self._task_metadata.keys()):
            if not self._base_manager.get(task_id):
                del self._task_metadata[task_id]
        return removed

    def _map_status(self, base_status: str) -> TaskStatus:
        """映射基础状态到产品状态"""
        mapping = {
            "running": "running",
            "completed": "completed",
            "failed": "failed",
            "cancelled": "cancelled",
        }
        return mapping.get(base_status, "running")  # type: ignore

    def _notify_task_update(self, snapshot: RuntimeTaskSnapshot):
        """通知任务更新"""
        if self._on_task_update:
            try:
                self._on_task_update(snapshot)
            except Exception:
                # 不让通知回调影响任务管理
                pass
