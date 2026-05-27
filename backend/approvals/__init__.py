"""
审批模块初始化

导出核心类和类型
"""
from backend.approvals.models import (
    ProductApproval,
    ApprovalSummary,
    ApprovalDiffPayload,
    ApprovalDiffFile,
    ApprovalDiffStats,
    ApprovalKind,
    RiskLevel,
)
from backend.approvals.manager import ProductApprovalManager

__all__ = [
    "ProductApproval",
    "ApprovalSummary",
    "ApprovalDiffPayload",
    "ApprovalDiffFile",
    "ApprovalDiffStats",
    "ApprovalKind",
    "RiskLevel",
    "ProductApprovalManager",
]
