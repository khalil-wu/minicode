"""Command-backed bearer credentials, following Source's external bearer owner."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import time

import httpx

from backend.application_network import application_network_policy


def command_auth_config(value, *, cwd: Path) -> dict:
    command = value["command"]
    args = value.get("args", [])
    timeout = value.get("timeout_ms", 5000)
    interval = value.get("refresh_interval_ms", 300000)
    if not isinstance(command, str) or not command.strip():
        raise ValueError("Provider auth command must be a non-empty string")
    if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
        raise ValueError("Provider auth args must contain strings")
    if type(timeout) is not int or timeout <= 0:
        raise ValueError("Provider auth timeout_ms must be a positive integer")
    if type(interval) is not int or interval < 0:
        raise ValueError("Provider auth refresh_interval_ms must be a non-negative integer")
    directory = Path(value.get("cwd", cwd)).expanduser()
    if not directory.is_absolute():
        directory = cwd / directory
    program = Path(command)
    if not program.is_absolute() and len(program.parts) > 1:
        command = str(directory / program)
    return {"command": command, "args": list(args), "timeout_ms": timeout,
        "refresh_interval_ms": interval, "cwd": str(directory)}


class CommandBearerAuth(httpx.Auth):
    """One captured command and token cache shared by runtime and HTTP requests."""

    def __init__(self, config: dict, *, network_policy=None):
        self.config = deepcopy(config)
        self.network_policy = network_policy if network_policy is not None else application_network_policy()
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._fetched_at = 0.0
        self._generation = 0

    def with_network_policy(self, policy):
        if (self.network_policy.controller is policy.controller
                and self.network_policy.generation == policy.generation):
            return self
        return CommandBearerAuth(self.config, network_policy=policy)

    async def token(self, *, rejected_generation: int | None = None, force: bool = False) -> str:
        permit = self.network_policy.acquire_for_unsupported_sdk()
        async def resolve():
            async with self._lock:
                interval = self.config["refresh_interval_ms"] / 1000
                if self._token is not None and not force:
                    if rejected_generation is not None:
                        if rejected_generation != self._generation:
                            return self._token
                    elif interval == 0 or time.monotonic() - self._fetched_at < interval:
                        return self._token
                # Once rejected/expired, a failed refresh cannot republish that token.
                self._token = None
                self._token = await self._run()
                self._fetched_at = time.monotonic()
                self._generation += 1
                return self._token
        return await permit.run(resolve)

    async def _run(self) -> str:
        process = await asyncio.create_subprocess_exec(self.config["command"], *self.config["args"],
            cwd=self.config["cwd"], stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), self.config["timeout_ms"] / 1000)
        except TimeoutError:
            raise TimeoutError(f"Provider auth command timed out after {self.config['timeout_ms']} ms") from None
        finally:
            if process.returncode is None:
                process.kill()
                await process.communicate()
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise OSError(f"Provider auth command exited with {process.returncode}" + (f": {detail}" if detail else ""))
        try:
            token = stdout.decode("utf-8").strip()
        except UnicodeDecodeError:
            raise ValueError("Provider auth command wrote non-UTF-8 data") from None
        if not token:
            raise ValueError("Provider auth command produced an empty token")
        return token

    async def async_auth_flow(self, request):
        self.network_policy.acquire(str(request.url)).check()
        request.headers["authorization"] = "Bearer " + await self.token()
        generation = self._generation
        response = yield request
        if response.status_code == 401:
            await response.aread()
            request.headers["authorization"] = "Bearer " + await self.token(rejected_generation=generation)
            yield request
