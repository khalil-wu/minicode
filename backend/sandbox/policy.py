"""Sandbox policy — declares what a subprocess is allowed to do."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class SandboxPolicy:
    """Declarative policy for subprocess sandboxing.

    Attributes:
        writable_roots: Directories the process may write to.
        readable_roots: Additional directories the process may read (beyond writable).
        allow_network: Whether outbound network access is permitted.
        env_overrides: Extra env vars to inject into the subprocess.
        timeout: Max execution time in seconds.
    """

    writable_roots: tuple[Path, ...] = ()
    readable_roots: tuple[Path, ...] = ()
    allow_network: bool = False
    env_overrides: dict[str, str] = field(default_factory=dict)
    timeout: int = 120

    @classmethod
    def workspace_default(cls, workspace: Path, *, timeout: int = 120) -> SandboxPolicy:
        """Default policy: write to workspace, read system libs, no network."""
        return cls(
            writable_roots=(workspace,),
            readable_roots=(),
            allow_network=False,
            timeout=timeout,
        )

    @classmethod
    def permissive(cls, workspace: Path, *, timeout: int = 120) -> SandboxPolicy:
        """Permissive policy: write to workspace, allow network."""
        return cls(
            writable_roots=(workspace,),
            readable_roots=(),
            allow_network=True,
            timeout=timeout,
        )
