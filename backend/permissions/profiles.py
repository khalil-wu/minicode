from __future__ import annotations

import shutil
import sys
from typing import Literal

PermissionProductProfile = Literal["ask", "auto", "full_access"]
WorkspaceScope = Literal["computer", "project", "worktree"]
SandboxOsStatus = Literal["enforced", "app_layer", "disabled"]
SandboxNetworkStatus = Literal["restricted", "approval_required", "enabled"]


def permission_profile_for_mode(mode: str | None) -> PermissionProductProfile:
    normalized = str(mode or "").strip().lower()
    if normalized in {"bypass", "full_access", "full-access", "full access", "danger-full-access"}:
        return "full_access"
    if normalized in {"ask", "ask_permissions", "ask-permissions", "confirm", "plan"}:
        return "ask"
    return "auto"


def workspace_scope_for(
    *,
    workspace_root: object | None,
    worktree_path: object | None = None,
) -> WorkspaceScope:
    if str(worktree_path or "").strip():
        return "worktree"
    if str(workspace_root or "").strip():
        return "project"
    return "computer"


def _has_native_os_sandbox(platform_name: str) -> bool:
    if platform_name == "darwin":
        return shutil.which("sandbox-exec") is not None
    if platform_name.startswith("linux"):
        return shutil.which("unshare") is not None or shutil.which("firejail") is not None
    return False


def sandbox_status_for(
    profile: PermissionProductProfile | str,
    *,
    platform_name: str | None = None,
) -> dict[str, SandboxOsStatus | SandboxNetworkStatus]:
    normalized = permission_profile_for_mode(profile)
    if normalized == "full_access":
        return {"os": "disabled", "network": "enabled"}

    platform_value = platform_name or sys.platform
    os_status: SandboxOsStatus = "enforced" if _has_native_os_sandbox(platform_value) else "app_layer"
    return {"os": os_status, "network": "approval_required"}
