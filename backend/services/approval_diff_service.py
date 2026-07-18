from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ApprovalFileDiffResult:
    conversation_id: str
    tool_call_id: str
    path: str
    patch: str
    is_large: bool
    is_truncated: bool

    def to_payload(self) -> dict[str, Any]:
        return {
            "type": "approval.file_diff",
            "conversation_id": self.conversation_id,
            "tool_call_id": self.tool_call_id,
            "path": self.path,
            "patch": self.patch,
            "is_large": self.is_large,
            "is_truncated": self.is_truncated,
        }


def get_approval_file_diff(
    approval_diff_cache: dict[str, Any],
    *,
    tool_call_id: str,
    path: str,
    conversation_id: str = "",
) -> ApprovalFileDiffResult:
    clean_tool_call_id = str(tool_call_id or "").strip()
    clean_path = str(path or "").strip()
    if not clean_tool_call_id or not clean_path:
        raise ValueError("Approval file diff requires tool_call_id and path")

    payload = approval_diff_cache.get(clean_tool_call_id)
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, list):
        raise ValueError(f"Approval diff '{clean_tool_call_id}' is no longer available")

    matched = next(
        (item for item in files if isinstance(item, dict) and str(item.get("path", "")).strip() == clean_path),
        None,
    )
    if matched is None:
        raise ValueError(f"Approval diff file '{clean_path}' was not found")

    patch = matched.get("patch")
    if not isinstance(patch, str):
        raise ValueError(f"Approval diff patch for '{clean_path}' is unavailable")

    return ApprovalFileDiffResult(
        conversation_id=str(conversation_id or ""),
        tool_call_id=clean_tool_call_id,
        path=clean_path,
        patch=patch,
        is_large=bool(matched.get("is_large")),
        is_truncated=bool(matched.get("is_truncated")),
    )
