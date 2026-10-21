"""Source's bounded AWS credential export and explicit reauthentication."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
from weakref import WeakValueDictionary
from backend.application_network import application_network_policy


def command_config(value: dict, *, refresh: bool = False) -> dict:
    command = value["command"]
    if refresh and command != "aws":
        raise ValueError("AWS auth refresh command must be `aws`")
    if not isinstance(command, str) or not command.strip() or command in {".", ".."} or (not Path(command).is_absolute() and Path(command).name != command):
        raise ValueError("AWS credential export command must be an absolute path or a bare executable name")
    timeout = value.get("timeout_ms", 300000 if refresh else 30000)
    if type(timeout) is not int or timeout <= 0:
        raise ValueError("AWS command timeout_ms must be a positive integer")
    args = value.get("args", [])
    if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
        raise ValueError("AWS command args must contain strings")
    return {"command": command, "args": list(args), "timeout_ms": timeout}


async def run_aws_command(config: dict, *, export: bool) -> bytes:
    process = await asyncio.create_subprocess_exec(config["command"], *config["args"],
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE if export else 2,
        stderr=asyncio.subprocess.DEVNULL if export else None,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))

    async def collect():
        output = bytearray()
        if export:
            while True:
                chunk = await process.stdout.read(min(4096, 65537 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > 65536:
                    raise ValueError("AWS credential export output exceeds 64 KiB")
        code = await process.wait()
        if code != 0:
            raise OSError(f"AWS {'credential export' if export else 'auth refresh'} command exited with {code}")
        return bytes(output)

    try:
        return await asyncio.wait_for(collect(), config["timeout_ms"] / 1000)
    except TimeoutError:
        raise TimeoutError(f"AWS {'credential export' if export else 'auth refresh'} command timed out after {config['timeout_ms']} ms") from None
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()


class AwsCredentialExport:
    def __init__(self, config: dict, *, network_policy=None):
        self.network_policy = network_policy if network_policy is not None else application_network_policy()
        self.config = command_config(config)
        self.lock = asyncio.Lock()
        self.cached = None
        self.refresh_at = datetime.min.replace(tzinfo=UTC)
        self.recovery_generation = 0

    async def get(self):
        permit = self.network_policy.acquire_for_unsupported_sdk()
        return await permit.run(self._get)

    async def _get(self):
        async with self.lock:
            now = datetime.now(UTC)
            if self.cached is not None and now < self.refresh_at:
                return self.cached
            await self._export()
            return self.cached

    async def recover(self, refresher):
        permit = self.network_policy.acquire_for_unsupported_sdk()
        return await permit.run(lambda: self._recover(refresher))

    async def _recover(self, refresher):
        generation = self.recovery_generation
        async with self.lock:
            if self.recovery_generation != generation:
                return
            # Keep the original cache unavailable across both login and export.
            # Failure/cancellation must not publish the rejected credentials again.
            self.cached = None
            if refresher is not None:
                await refresher.refresh(refresher.generation)
            await self._export()
            self.recovery_generation += 1

    async def _export(self):
        raw = await run_aws_command(self.config, export=True)
        now = datetime.now(UTC)
        try:
            value = json.loads(raw)
            value = value.get("Credentials", value)
            access, secret = value["AccessKeyId"], value["SecretAccessKey"]
            token = value.get("SessionToken")
            expiration = value.get("Expiration")
            if not isinstance(access, str) or not isinstance(secret, str) or (token is not None and not isinstance(token, str)):
                raise TypeError()
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ValueError("AWS credential export returned invalid credentials JSON") from None
        if not access.strip() or not secret.strip():
            raise ValueError("AWS credential export returned empty access keys")
        refresh_at = now + timedelta(hours=1)
        if expiration is not None:
            try:
                expires = datetime.fromisoformat(expiration.replace("Z", "+00:00"))
                if expires.tzinfo is None:
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise ValueError("AWS credential export Expiration must be an RFC 3339 timestamp") from None
            if expires <= now:
                raise ValueError("AWS credential export returned expired credentials")
            refresh_at = max(now, expires - timedelta(minutes=5))
        from botocore.credentials import ReadOnlyCredentials
        self.cached = ReadOnlyCredentials(access, secret, token)
        self.refresh_at = refresh_at


class AwsAuthRefresh:
    def __init__(self, config: dict, *, network_policy=None):
        self.network_policy = network_policy if network_policy is not None else application_network_policy()
        self.config = command_config(config, refresh=True)
        self.lock = asyncio.Lock()
        self.generation = 0

    async def refresh(self, expected_generation: int):
        permit = self.network_policy.acquire_for_unsupported_sdk()
        return await permit.run(lambda: self._refresh(expected_generation))

    async def _refresh(self, expected_generation: int):
        async with self.lock:
            if self.generation != expected_generation:
                return False
            await run_aws_command(self.config, export=False)
            self.generation += 1
            return True


_EXPORTERS: WeakValueDictionary[tuple, AwsCredentialExport] = WeakValueDictionary()
_REFRESHERS: WeakValueDictionary[tuple, AwsAuthRefresh] = WeakValueDictionary()


def credential_export(aws: dict, *, network_policy=None) -> AwsCredentialExport | None:
    config = aws.get("credential_export")
    if config is None:
        return None
    policy = network_policy if network_policy is not None else application_network_policy()
    key = (hashlib.sha256(json.dumps(aws, sort_keys=True).encode()).hexdigest(), policy.controller, policy.generation)
    exporter = _EXPORTERS.get(key)
    if exporter is None:
        exporter = AwsCredentialExport(config, network_policy=policy)
        _EXPORTERS[key] = exporter
    return exporter


def auth_refresh(aws: dict, *, network_policy=None) -> AwsAuthRefresh | None:
    config = aws.get("auth_refresh")
    if config is None:
        return None
    policy = network_policy if network_policy is not None else application_network_policy()
    key = (hashlib.sha256(json.dumps(aws, sort_keys=True).encode()).hexdigest(), policy.controller, policy.generation)
    refresher = _REFRESHERS.get(key)
    if refresher is None:
        refresher = AwsAuthRefresh(config, network_policy=policy)
        _REFRESHERS[key] = refresher
    return refresher
