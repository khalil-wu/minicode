"""Sandbox execution result."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SandboxResult:
    """Result of a sandboxed subprocess execution."""

    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False
    cancelled: bool = False
    sandbox_unavailable: bool = False
    stdout_path: str = ""
    stderr_path: str = ""
    stdout_total_bytes: int = 0
    stderr_total_bytes: int = 0
    # Set when the run ended by termination and the process tree's exit could
    # not be proven. Owners must retain their recovery evidence instead of
    # reporting a completed teardown.
    cleanup_pending: bool = False
    cleanup_reason: str = ""

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.cancelled
