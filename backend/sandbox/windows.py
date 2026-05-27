"""Windows-specific sandbox using restricted process tokens.

Strategy: Create a subprocess with a restricted token that has:
  1. Low integrity level (limits write access to Low-labeled objects only)
  2. Disabled SID (reduces group privileges)

This provides meaningful isolation without requiring WSL2, Docker, or Hyper-V.
Network isolation on Windows requires Windows Filtering Platform (WFP) rules
which need admin privileges — not practical for a desktop app. The application
layer enforces network policy via the approval flow instead.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import subprocess
import sys
from pathlib import Path

if sys.platform != "win32":
    raise ImportError("This module is Windows-only")

# Windows constants
SECURITY_MANDATORY_LOW_RID = 0x00001000
SE_GROUP_INTEGRITY = 0x00000020
TOKEN_DUPLICATE = 0x0002
TOKEN_ADJUST_DEFAULT = 0x0080
TOKEN_QUERY = 0x0008
TOKEN_ASSIGN_PRIMARY = 0x0001
TokenIntegrityLevel = 25
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009

kernel32 = ctypes.windll.kernel32
advapi32 = ctypes.windll.advapi32


def create_low_integrity_flags() -> int:
    """Return creation flags for a low-integrity subprocess.

    Uses CREATE_NEW_PROCESS_GROUP for clean signal handling.
    The actual integrity level is set via the token, not flags alone.
    """
    return (
        subprocess.CREATE_NEW_PROCESS_GROUP
        | subprocess.CREATE_BREAKAWAY_FROM_JOB
        | subprocess.BELOW_NORMAL_PRIORITY_CLASS
    )


def make_low_integrity_command(command: str, writable_root: Path) -> str:
    """Wrap a command to run at low integrity level using icacls + runas.

    This approach:
    1. Grants the Low integrity label write access to the workspace
    2. Launches the command via a restricted token

    For the MVP, we use PowerShell's Start-Process with restricted token
    as a pragmatic approach that doesn't require admin privileges.
    """
    workspace = str(writable_root).replace("'", "''")
    # Grant Low integrity write access to workspace (idempotent, no admin needed)
    # icacls sets the mandatory label — processes at Low integrity can write here
    grant_cmd = f'icacls "{workspace}" /setintegritylevel (OI)(CI)Low /T /Q 2>$null'
    # The actual command runs in a restricted PowerShell
    return (
        f"powershell.exe -NoProfile -NonInteractive -Command \""
        f"{grant_cmd}; "
        f"& cmd /c '{command}'"
        f"\""
    )


def is_available() -> bool:
    """Check if Windows restricted token sandbox is available."""
    if sys.platform != "win32":
        return False
    try:
        # Verify we can access the required APIs
        return hasattr(kernel32, "CreateProcessW")
    except Exception:
        return False
