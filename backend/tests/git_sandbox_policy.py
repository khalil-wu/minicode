"""Git-hook tests declare system read access while keeping write isolation."""
from dataclasses import replace

from backend.sandbox.policy import (
    FileSystemAccessMode, FileSystemPath, FileSystemSandboxEntry,
    FileSystemSandboxPolicy, FileSystemSpecialPath, PermissionProfile, SandboxPolicy,
)


def readable_host_git_policy(policy: SandboxPolicy) -> SandboxPolicy:
    profile = policy.permission_profile
    filesystem = FileSystemSandboxPolicy.restricted([
        *profile.file_system.entries,
        FileSystemSandboxEntry(FileSystemPath.special(FileSystemSpecialPath.ROOT), FileSystemAccessMode.READ),
    ], glob_scan_max_depth=profile.file_system.glob_scan_max_depth)
    return replace(policy, permission_profile=PermissionProfile.managed(filesystem, network=profile.network))
