from __future__ import annotations

import re
import sys


_WINDOWS_BARE_CURL_RE = re.compile(r"(?P<prefix>^\s*)curl(?P<suffix>\s|$)", re.IGNORECASE)


def normalize_windows_shell_command(command: str, *, platform: str | None = None) -> str:
    """Prefer native curl on Windows instead of PowerShell's curl alias.

    In Windows PowerShell, ``curl`` resolves to ``Invoke-WebRequest``. That makes
    common curl flags such as ``-m`` fail with ambiguous PowerShell parameters,
    so generated shell commands should call ``curl.exe`` explicitly.
    """

    target_platform = sys.platform if platform is None else platform
    if target_platform != "win32":
        return command
    return _WINDOWS_BARE_CURL_RE.sub(
        lambda match: f"{match.group('prefix')}curl.exe{match.group('suffix')}",
        command,
        count=1,
    )


def windows_powershell_native_tool_alias_prelude() -> str:
    """PowerShell startup snippet that keeps familiar native CLI names usable."""

    return (
        "if (Get-Command curl.exe -ErrorAction SilentlyContinue) { "
        "Set-Alias -Name curl -Value curl.exe -Scope Local -Force; "
        "} "
    )
