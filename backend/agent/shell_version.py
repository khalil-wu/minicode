"""Optional Source PowerShell version context for one local environment."""
from __future__ import annotations

import asyncio
import re

from backend.agent.environment_selection import StartingTurnEnvironment, TurnEnvironment
from backend.subprocesses import communicate, spawn_exec


_POWERSHELL_VERSIONS: dict[str, str | None] = {}
_VERSION_TIMEOUT = 2.0


def parse_powershell_version(output: bytes) -> str | None:
    if len(output) > 64:
        return None
    try:
        parts = output.decode("utf-8").strip().split(".")
    except UnicodeDecodeError:
        return None
    if len(parts) < 2 or any(re.fullmatch(r"\+?[0-9]+", part) is None for part in parts[:2]):
        return None
    major, minor = (int(part) for part in parts[:2])
    return f"{major}.{minor}" if major <= 65535 and minor <= 65535 else None


async def powershell_version(path: str) -> str | None:
    if path in _POWERSHELL_VERSIONS:
        return _POWERSHELL_VERSIONS[path]
    try:
        process = await spawn_exec(path, "-NoProfile", "-NonInteractive", "-Command",
            "$PSVersionTable.PSVersion.ToString()", stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        output, _ = await communicate(process, timeout=_VERSION_TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        # Source's optional version probe caches failed observations as absent.
        version = None
    else:
        version = parse_powershell_version(output) if process.returncode == 0 else None
    _POWERSHELL_VERSIONS[path] = version
    return version


async def shell_version_for_environment(snapshot, *, enabled: bool) -> str | None:
    if not enabled or snapshot is None:
        return None
    if any(isinstance(environment, StartingTurnEnvironment) for environment in snapshot.environments):
        return None
    ready = [environment for environment in snapshot.environments if isinstance(environment, TurnEnvironment)]
    if len(ready) != 1:
        return None
    environment, = ready
    if environment.is_remote:
        return None
    shell = environment.shell
    if shell is None or shell["name"] != "powershell":
        return None
    return await powershell_version(shell["path"])
