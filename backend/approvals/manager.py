"""
产品级审批管理器

根据 newplan.md 第 11 节实现
管理审批的创建、查询、响应
"""
from __future__ import annotations

import logging
from typing import Callable, Literal
from uuid import uuid4

from backend.approvals.models import (
    ProductApproval,
    ApprovalSummary,
    ApprovalKind,
    RiskLevel,
    ApprovalStatus,
    ApprovalDiffPayload,
)

logger = logging.getLogger(__name__)


class ProductApprovalManager:
    """
    产品级审批管理器

    职责：
    - 创建产品级审批对象
    - 管理审批生命周期
    - 提供审批查询和过滤
    - 触发审批更新通知
    """

    def __init__(
        self,
        on_approval_update: Callable[[ProductApproval], None] | None = None,
    ):
        self._approvals: dict[str, ProductApproval] = {}
        self._on_approval_update = on_approval_update
        self._session_id: str | None = None
        self._conversation_id: str | None = None

    def set_session_context(self, session_id: str, conversation_id: str | None = None):
        """设置当前会话上下文"""
        self._session_id = session_id
        self._conversation_id = conversation_id

    def create_approval(
        self,
        *,
        kind: ApprovalKind,
        title: str,
        summary: str,
        tool_name: str | None = None,
        risk_level: RiskLevel | None = None,
        task_id: str | None = None,
        diff: ApprovalDiffPayload | None = None,
        guidance_supported: bool = True,
    ) -> ProductApproval:
        """创建审批"""
        from datetime import UTC, datetime

        approval_id = f"approval_{uuid4().hex[:12]}"

        approval = ProductApproval(
            id=approval_id,
            kind=kind,
            title=title,
            tool_name=tool_name,
            risk_level=risk_level,
            summary=summary,
            requested_at=datetime.now(UTC).isoformat(),
            session_id=self._session_id,
            conversation_id=self._conversation_id,
            task_id=task_id,
            guidance_supported=guidance_supported,
            diff=diff,
        )

        self._approvals[approval_id] = approval
        self._notify_approval_update(approval)
        return approval

    def get_approval(self, approval_id: str) -> ProductApproval | None:
        """获取审批"""
        return self._approvals.get(approval_id)

    def list_approvals(
        self,
        *,
        kind_filter: ApprovalKind | None = None,
        task_id_filter: str | None = None,
        status_filter: ApprovalStatus | None = None,
    ) -> list[ProductApproval]:
        """列出审批"""
        approvals = []
        for approval in self._approvals.values():
            # 类型过滤
            if kind_filter and approval.kind != kind_filter:
                continue

            # 任务过滤
            if task_id_filter and approval.task_id != task_id_filter:
                continue

            # 状态过滤
            if status_filter and approval.status != status_filter:
                continue

            approvals.append(approval)

        # 按请求时间倒序
        approvals.sort(key=lambda a: a.requested_at, reverse=True)
        return approvals

    def get_approval_summary(self) -> ApprovalSummary:
        """获取审批摘要"""
        approvals = self.list_approvals()
        return ApprovalSummary.from_approvals(approvals)

    def get_pending_approvals(self) -> list[ProductApproval]:
        """获取待处理的审批"""
        return self.list_approvals(status_filter="pending")

    def resolve_approval(
        self,
        approval_id: str,
        action: Literal["approve", "reject"],
        *,
        guidance: str | None = None,
    ) -> ProductApproval | None:
        """标记审批结果并保留历史记录"""
        approval = self._approvals.get(approval_id)
        if approval is None:
            return None

        if action == "approve":
            approval.status = "approved"
        elif action == "reject":
            approval.status = "rejected"
        else:
            raise ValueError("action must be 'approve' or 'reject'")

        self._notify_approval_update(approval)
        return approval

    def remove_approval(self, approval_id: str) -> bool:
        """移除审批（审批完成后）"""
        if approval_id in self._approvals:
            del self._approvals[approval_id]
            return True
        return False

    def clear_approvals(self):
        """清空所有审批"""
        self._approvals.clear()

    def _notify_approval_update(self, approval: ProductApproval):
        """通知审批更新"""
        if self._on_approval_update:
            try:
                self._on_approval_update(approval)
            except Exception:
                # 通知回调不应影响审批管理
                logger.exception(
                    "Product approval update callback failed for approval %s (%s)",
                    approval.id,
                    approval.title,
                )
