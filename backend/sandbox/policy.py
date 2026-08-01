"""Sandbox policy — declares what a subprocess is allowed to do."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class SandboxPolicy:
    """Declarative policy for subprocess sandboxing.

    Attributes:
        workspace_root: Project root mounted as the command's readable working tree.
        writable_roots: Directories the process may write to.
        readable_roots: Additional directories the process may read (beyond writable).
        allow_network: Whether outbound network access is permitted.
        disable_os_sandbox: Whether to skip platform sandbox wrappers entirely.
        env_overrides: Extra env vars to inject into the subprocess.
        timeout: Optional execution deadline in seconds. ``None`` (or the
            legacy value ``0``) means no command-local deadline.
    """

    workspace_root: Path | None = None
    writable_roots: tuple[Path, ...] = ()
    readable_roots: tuple[Path, ...] = ()
    allow_network: bool = False
    disable_os_sandbox: bool = False
    env_overrides: dict[str, str] = field(default_factory=dict)
    timeout: float | None = None

    @classmethod
    def workspace_default(cls, workspace: Path, *, timeout: float | None = None) -> SandboxPolicy:
        """Default policy: write to workspace, read system libs, no network."""
        return cls(
            workspace_root=workspace,
            writable_roots=(workspace,),
            readable_roots=(),
            allow_network=False,
            timeout=timeout,
        )

    @classmethod
    def permissive(cls, workspace: Path, *, timeout: float | None = None) -> SandboxPolicy:
        """Permissive policy: write to workspace, allow network."""
        return cls(
            workspace_root=workspace,
            writable_roots=(workspace,),
            readable_roots=(),
            allow_network=True,
            timeout=timeout,
        )

    @classmethod
    def danger_full_access(
        cls,
        *,
        timeout: float | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> SandboxPolicy:
        """No OS sandbox and network allowed; used only for explicit bypass mode."""
        return cls(
            writable_roots=(),
            readable_roots=(),
            allow_network=True,
            disable_os_sandbox=True,
            env_overrides=dict(env_overrides or {}),
            timeout=timeout,
        )
