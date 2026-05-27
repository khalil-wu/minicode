"""
产品级审批模型

根据 newplan.md 第 11 节实现
审批统一入口和产品级对象定义
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


ApprovalKind = Literal["approval_request", "control_request"]
RiskLevel = Literal["low", "medium", "high"]


@dataclass
class ApprovalDiffStats:
    """Diff 统计信息"""
    files_count: int
    additions: int
    deletions: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "files_count": self.files_count,
            "additions": self.additions,
            "deletions": self.deletions,
        }


@dataclass
class ApprovalDiffFile:
    """单个文件的 Diff 信息"""
    path: str
    old_path: str | None = None
    language: str | None = None
    status: str | None = None
    additions: int | None = None
    deletions: int | None = None
    patch: str | None = None
    is_binary: bool = False
    is_large: bool = False
    is_truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "old_path": self.old_path,
            "language": self.language,
            "status": self.status,
            "additions": self.additions,
            "deletions": self.deletions,
            "patch": self.patch,
            "is_binary": self.is_binary,
            "is_large": self.is_large,
            "is_truncated": self.is_truncated,
        }


@dataclass
class ApprovalDiffPayload:
    """审批 Diff 载荷"""
    format: Literal["structured", "raw"]
    stats: ApprovalDiffStats | None = None
    files: list[ApprovalDiffFile] = field(default_factory=list)
    raw: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "stats": self.stats.to_dict() if self.stats else None,
            "files": [f.to_dict() for f in self.files],
            "raw": self.raw,
        }

    @property
    def is_large(self) -> bool:
        """判断是否为大型 Diff（需要分页）"""
        if self.stats:
            # 超过 10 个文件
            if self.stats.files_count > 10:
                return True
            # patch 总量超过 100KB
            total_patch_size = sum(
                len(f.patch or "") for f in self.files
            )
            if total_patch_size > 100 * 1024:
                return True
        return False


@dataclass
class ProductApproval:
    """
    产品级审批对象

    根据 newplan.md 第 8.4 节定义
    统一审批入口，归一化 approval_request 和 control_request
    """
    id: str
    kind: ApprovalKind
    title: str
    tool_name: str | None
    risk_level: RiskLevel | None
    summary: str
    requested_at: str
    session_id: str | None = None
    conversation_id: str | None = None
    task_id: str | None = None
    guidance_supported: bool = True
    diff: ApprovalDiffPayload | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "tool_name": self.tool_name,
            "risk_level": self.risk_level,
            "summary": self.summary,
            "requested_at": self.requested_at,
            "session_id": self.session_id,
            "conversation_id": self.conversation_id,
            "task_id": self.task_id,
            "guidance_supported": self.guidance_supported,
            "diff": self.diff.to_dict() if self.diff else None,
        }

    @property
    def has_diff(self) -> bool:
        """是否包含 Diff"""
        return self.diff is not None

    @property
    def is_large_diff(self) -> bool:
        """是否为大型 Diff"""
        return self.diff.is_large if self.diff else False


@dataclass
class ApprovalSummary:
    """审批摘要统计"""
    total: int = 0
    pending: int = 0
    approved: int = 0
    rejected: int = 0

    @classmethod
    def from_approvals(cls, approvals: list[ProductApproval]) -> ApprovalSummary:
        """从审批列表生成摘要"""
        return cls(
            total=len(approvals),
            pending=len(approvals),  # 所有在列表中的都是待处理
            approved=0,
            rejected=0,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "pending": self.pending,
            "approved": self.approved,
            "rejected": self.rejected,
        }
