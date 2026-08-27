from __future__ import annotations

from pathlib import Path
import shutil
import sys
import time
from threading import Lock
from typing import Any, Literal

PermissionProductProfile = Literal["confirm", "plan", "auto", "bypass"]
WorkspaceScope = Literal["computer", "project", "worktree"]
SandboxOsStatus = Literal["enforced", "app_layer", "disabled"]
SandboxNetworkStatus = Literal["restricted", "approval_required", "enabled"]

_native_sandbox_cache: dict[str, bool] = {}
_native_sandbox_cache_lock = Lock()


def permission_profile_for_mode(mode: str | None) -> PermissionProductProfile:
    normalized = str(mode or "").strip().lower()
    if normalized == "bypass":
        return "bypass"
    if normalized in {"confirm", "plan"}:
        return normalized
    if normalized == "auto":
        return "auto"
    raise ValueError(f"Unsupported permission mode: {mode!r}")


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
    if normalized == "bypass":
        return {"os": "disabled", "network": "enabled"}

    platform_value = platform_name or sys.platform
    os_status: SandboxOsStatus = (
        "enforced" if _has_native_os_sandbox(platform_value) else "app_layer"
    )
    return {"os": os_status, "network": "approval_required"}


def sandbox_capability_for_context(
    workspace_root: str | Path,
    permission_context: Any,
) -> dict[str, Any]:
    """Project the same MiniCode policy used for commands into diagnostics.

    This is deliberately a probe result, not a product-profile guess.  Full
    bypass is reported as no MiniCode sandbox requested; external sandbox is
    reported as externally managed and unknown to this process.
    """

    from backend.sandbox.policy import sandbox_policy_from_config_snapshot
    from backend.sandbox.runner import SandboxRunner

    workspace = Path(workspace_root).expanduser().resolve()
    mode = str(getattr(permission_context, "sandbox_mode", "") or "workspace-write").strip().lower()
    if str(getattr(permission_context, "mode", "") or "").strip().lower() == "bypass":
        mode = "danger-full-access"
    policy = sandbox_policy_from_config_snapshot(
        workspace,
        sandbox_mode=mode,
        managed_sandbox_settings={
            "enabled": mode not in {"danger-full-access", "external-sandbox"},
            "allowUnsandboxedCommands": bool(getattr(permission_context, "allow_unsandboxed_commands", False)),
            "failIfUnavailable": bool(getattr(permission_context, "sandbox_fail_if_unavailable", True)),
        },
        workspace_write_settings={
            "network_access": bool(getattr(permission_context, "allow_network", False)),
        },
        filesystem_constraints=getattr(permission_context, "filesystem_constraints", {}) or {},
    )
    resolved = policy.resolve(cwd=workspace)
    requested_filesystem = resolved.enforcement.value == "managed"
    requested_network = not resolved.allow_network
    requested_deny_read = bool(resolved.has_denied_read_restrictions)
    requested_protected = bool(policy.protect_workspace_metadata)
    capability = SandboxRunner(policy).capability(cwd=workspace)
    if resolved.enforcement.value == "disabled":
        backend_available: bool | None = None
        enforcement = "disabled"
        isolated = {"filesystem": False, "network": False, "deny_read": False, "protected_paths": False}
        fail_closed = False
        unavailable_action = "none"
    elif resolved.enforcement.value == "external":
        backend_available = None
        enforcement = "external"
        isolated = {"filesystem": None, "network": None, "deny_read": None, "protected_paths": None}
        fail_closed = True
        unavailable_action = "external_backend"
    else:
        backend_available = bool(capability.available)
        enforcement = "managed"
        isolated = {
            "filesystem": bool(capability.filesystem_isolated),
            "network": bool(capability.network_isolated),
            "deny_read": bool(capability.deny_read_isolated),
            "protected_paths": bool(capability.protected_paths_isolated),
        }
        # Report what the command path actually does when the backend is
        # missing, not a fixed "reject". A required sandbox stops the turn at
        # preflight; otherwise commands run under the permission policy without
        # OS isolation, and only a managed no-fallback policy rejects them.
        if capability.available:
            fail_closed = False
            unavailable_action = "enforce_policy"
        elif policy.preflight_required:
            fail_closed = True
            unavailable_action = "reject_turn"
        elif policy.allow_unsandboxed_commands:
            fail_closed = False
            unavailable_action = "run_unsandboxed"
        else:
            fail_closed = True
            unavailable_action = "reject_command"
    return {
        "policy_configured": True,
        "probe_status": "ready",
        "enforcement": enforcement,
        "requested": {
            "filesystem": requested_filesystem,
            "network": requested_network,
            "deny_read": requested_deny_read,
            "protected_paths": requested_protected,
        },
        "backend_available": backend_available,
        "backend": capability.backend,
        "filesystem_isolated": isolated["filesystem"],
        "network_isolated": isolated["network"],
        "deny_read_isolated": isolated["deny_read"],
        "protected_paths_isolated": isolated["protected_paths"],
        "fail_closed": fail_closed,
        "unavailable_action": unavailable_action,
        "reason": capability.reason,
        "policy_limitations": list(getattr(policy, "policy_limitations", ()) or ()),
        "probed_at": time.time(),
    }
