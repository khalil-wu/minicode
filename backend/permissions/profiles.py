from __future__ import annotations

from pathlib import Path
import shutil
import sys
from threading import Lock
from typing import Literal

PermissionProductProfile = Literal["ask", "auto", "full_access"]
WorkspaceScope = Literal["computer", "project", "worktree"]
SandboxOsStatus = Literal["enforced", "app_layer", "disabled"]
SandboxNetworkStatus = Literal["restricted", "approval_required", "enabled"]

_native_sandbox_cache: dict[str, bool] = {}
_native_sandbox_cache_lock = Lock()


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


def refresh_native_os_sandbox(platform_name: str | None = None) -> bool:
    """Probe the effective sandbox off the request path and cache the result."""

    platform_value = platform_name or sys.platform
    available = False
    if platform_value != sys.platform:
        if platform_value == "darwin":
            available = shutil.which("sandbox-exec") is not None
        elif platform_value.startswith("linux"):
            available = shutil.which("bwrap") is not None
    else:
        try:
            from backend.sandbox.policy import SandboxPolicy
            from backend.sandbox.runner import SandboxRunner

            workspace = Path.cwd().resolve()
            capability = SandboxRunner(SandboxPolicy.workspace_default(workspace)).capability()
            available = bool(capability.available and capability.filesystem_isolated)
        except Exception:
            available = False
    with _native_sandbox_cache_lock:
        _native_sandbox_cache[platform_value] = available
    return available


def _has_native_os_sandbox(platform_name: str) -> bool:
    """Return a cached capability without blocking a WebSocket/API snapshot."""

    with _native_sandbox_cache_lock:
        cached = _native_sandbox_cache.get(platform_name)
    if cached is not None:
        return cached
    if platform_name != sys.platform:
        if platform_name == "darwin":
            return shutil.which("sandbox-exec") is not None
        if platform_name.startswith("linux"):
            return shutil.which("bwrap") is not None
        return False
    # Current-host probes can invoke Docker/Podman. Until startup prewarming
    # finishes, report the conservative app-layer state instead of blocking a
    # session snapshot or claiming an unverified security boundary.
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
    os_status: SandboxOsStatus = (
        "enforced" if _has_native_os_sandbox(platform_value) else "app_layer"
    )
    return {"os": os_status, "network": "approval_required"}
