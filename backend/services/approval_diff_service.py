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
    turn_id: str = ""
    workspace_root: str = ""

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "type": "approval.file_diff",
            "conversation_id": self.conversation_id,
            "tool_call_id": self.tool_call_id,
            "path": self.path,
            "patch": self.patch,
            "is_large": self.is_large,
            "is_truncated": self.is_truncated,
        }
        if self.turn_id:
            payload["turn_id"] = self.turn_id
        if self.workspace_root:
            payload["workspace_root"] = self.workspace_root
        return payload


def get_approval_file_diff(
    approval_diff_cache: dict[str, Any],
    *,
    tool_call_id: str,
    path: str,
    conversation_id: str = "",
    turn_id: str = "",
) -> ApprovalFileDiffResult:
    clean_tool_call_id = str(tool_call_id or "").strip()
    clean_path = str(path or "").strip()
    if not clean_tool_call_id or not clean_path:
        raise ValueError("Approval file diff requires tool_call_id and path")

    payload = approval_diff_cache.get(clean_tool_call_id)
    owner = payload.get("_owner") if isinstance(payload, dict) else None
    if not isinstance(owner, dict):
        raise ValueError(f"Approval diff '{clean_tool_call_id}' has no owner metadata")
    expected_conversation_id = str(owner.get("conversation_id") or "").strip()
    expected_turn_id = str(owner.get("turn_id") or "").strip()
    if expected_conversation_id and str(conversation_id or "").strip() != expected_conversation_id:
        raise ValueError("Approval diff does not belong to the requested conversation")
    if expected_turn_id and str(turn_id or "").strip() != expected_turn_id:
        raise ValueError("Approval diff does not belong to the requested turn")
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
        turn_id=expected_turn_id,
        workspace_root=str(owner.get("workspace_root") or "").strip(),
    )
