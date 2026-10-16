"""The pinned Source Linux sandbox entry point in the native proxy binary.

Source owns the isolated network namespace, listener handoff, attribution,
bridge workers and inner seccomp stage. Python retains the original command
process group and its native network lease through their existing lifecycle.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from backend.network.native import SOURCE_COMMIT, packaged_network_directory
from backend.sandbox.policy import FileSystemAccessMode, ResolvedSandboxPolicy


def discover_runtime(executable: Path | None = None) -> tuple[Path | None, str]:
    binary = executable if executable is not None else packaged_network_directory() / "minicode-network"
    if not binary.is_file() or not os.access(binary, os.X_OK):
        return None, f"Pinned Linux network/sandbox runtime is not executable: {binary}"
    try:
        manifest = json.loads((binary.parent / "runtime.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return None, f"Cannot read the Linux network/sandbox runtime manifest: {error}"
    if (manifest.get("identity") != "minicode-network" or manifest.get("protocol") != 1
            or manifest.get("sourceCommit") != SOURCE_COMMIT
            or manifest.get("linux_sandbox_routing_protocol") != 1):
        return None, "Packaged network runtime does not contain the pinned Source Linux sandbox routing entry point"
    return binary, ""


def permission_profile(resolved: ResolvedSandboxPolicy) -> dict:
    """Materialize MiniCode's captured policy context before entering Source.

    The helper accepts a policy cwd, while MiniCode also captures multiple
    project roots, the user home and temporary directories. Use their already
    resolved native entries so a helper cannot rebind those selectors to cwd.
    """
    entries = []
    if resolved.root_access is not None:
        entries.append({"path": {"type": "special", "value": {"kind": "root"}},
                        "access": resolved.root_access.value})
    if resolved.include_platform_defaults:
        entries.append({"path": {"type": "special", "value": {"kind": "minimal"}}, "access": "read"})
    concrete = dict(resolved.resolved_entries)
    for writable in resolved.writable_roots:
        for path in (*writable.read_only_subpaths,
                *(writable.root / name for name in writable.protected_metadata_names)):
            concrete.setdefault(path, FileSystemAccessMode.READ)
    entries.extend({"path": {"type": "path", "path": str(path)}, "access": access.value}
                   for path, access in concrete.items())
    entries.extend({"path": {"type": "glob_pattern", "pattern": pattern}, "access": "deny"}
                   for pattern in resolved.unreadable_globs)
    filesystem = {"type": "restricted", "entries": entries}
    if resolved.glob_scan_max_depth is not None:
        filesystem["glob_scan_max_depth"] = resolved.glob_scan_max_depth
    return {"type": "managed", "file_system": filesystem,
            "network": "enabled" if resolved.allow_network else "restricted"}


def command(binary: Path, resolved: ResolvedSandboxPolicy, *, policy_cwd: Path,
        command_cwd: Path, argv: list[str]) -> list[str]:
    return [str(binary), "--sandbox-policy-cwd", str(policy_cwd),
        "--command-cwd", str(command_cwd), "--permission-profile",
        json.dumps(permission_profile(resolved), ensure_ascii=False), "--managed-network",
        json.dumps(dict(resolved.managed_network or {})), "--", *argv]
