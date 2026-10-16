"""Pinned Source environments.toml/provider startup snapshot."""
from __future__ import annotations

import ipaddress
import math
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .errors import ExecutorProtocolError


@dataclass(frozen=True)
class EnvironmentSpec:
    id: str
    url: str | None = None
    auth_bearer_token: str | None = field(default=None, repr=False)
    program: str | None = None
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict, repr=False)
    cwd: Path | None = None
    connect_timeout_sec: float = 10
    initialize_timeout_sec: float = 10
    required_skills: tuple[str, ...] = ()


@dataclass(frozen=True)
class EnvironmentSnapshot:
    environments: tuple[EnvironmentSpec, ...]
    include_local: bool
    default_environment_id: str | None


def validate_environment_id(environment_id: str) -> None:
    if environment_id == "local" or environment_id.lower() == "none":
        raise ExecutorProtocolError(f"environment id `{environment_id}` is reserved")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", environment_id):
        raise ExecutorProtocolError("environment id must contain 1–64 ASCII letters, numbers, '-' or '_'")


def validate_websocket_url(url: str, token: str | None) -> str:
    url = url.strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname or parsed.fragment:
        raise ExecutorProtocolError("environment url must be a valid ws:// or wss:// URL")
    # Source only transmits trusted headers over TLS or an actual loopback URL.
    if token is not None and parsed.scheme != "wss":
        host = parsed.hostname
        loopback = host.lower() == "localhost"
        if not loopback:
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = False
        if not loopback:
            raise ExecutorProtocolError("exec-server WebSocket headers require wss:// or a loopback destination")
    if token is not None and any(character in token for character in "\r\n"):
        raise ExecutorProtocolError("invalid exec-server Authorization header")
    return url


def _unknown_fields(data: Mapping[str, object], allowed: set[str], scope: str) -> None:
    unknown = data.keys() - allowed
    if unknown:
        raise ExecutorProtocolError(f"unknown {scope} fields: {', '.join(sorted(unknown))}")


def _duration(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ExecutorProtocolError("environment timeouts must be finite nonnegative seconds")
    return float(value)


def parse_environment_spec(data: Mapping[str, object], config_dir: Path) -> EnvironmentSpec:
    _unknown_fields(data, {"id", "url", "auth_bearer_token", "program", "args", "env", "cwd",
                           "skills", "connect_timeout_sec", "initialize_timeout_sec"}, "environment")
    environment_id = data["id"]
    if not isinstance(environment_id, str):
        raise ExecutorProtocolError("environment id must be a string")
    validate_environment_id(environment_id)
    url, program = data.get("url"), data.get("program")
    if (url is None) == (program is None):
        raise ExecutorProtocolError(f"environment `{environment_id}` must set exactly one of url or program")
    if program is None and any(key in data for key in ("args", "env", "cwd")):
        raise ExecutorProtocolError(f"environment `{environment_id}` args, env, and cwd require program")
    if url is None and any(key in data for key in ("auth_bearer_token", "connect_timeout_sec")):
        raise ExecutorProtocolError(f"environment `{environment_id}` auth_bearer_token and connect_timeout_sec require url")
    skills = data.get("skills", {})
    if not isinstance(skills, dict):
        raise ExecutorProtocolError("environment skills must be a table")
    _unknown_fields(skills, {"required"}, "environment skills")
    required = skills.get("required", [])
    if not isinstance(required, list) or not all(isinstance(name, str) for name in required):
        raise ExecutorProtocolError("environment skills.required must be an array of strings")
    args, env = data.get("args", []), data.get("env", {})
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise ExecutorProtocolError("environment args must be an array of strings")
    if not isinstance(env, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in env.items()):
        raise ExecutorProtocolError("environment env must be a string table")
    token = data.get("auth_bearer_token")
    if token is not None and not isinstance(token, str):
        raise ExecutorProtocolError("environment auth_bearer_token must be a string")
    if url is not None:
        if not isinstance(url, str):
            raise ExecutorProtocolError("environment url must be a string")
        url = validate_websocket_url(url, token)
    if program is not None:
        if not isinstance(program, str) or not program.strip():
            raise ExecutorProtocolError("environment program cannot be empty")
        program = program.strip()
    cwd = data.get("cwd")
    if cwd is not None:
        if not isinstance(cwd, str):
            raise ExecutorProtocolError("environment cwd must be a string")
        cwd = Path(cwd)
        if not cwd.is_absolute():
            cwd = config_dir / cwd
    return EnvironmentSpec(environment_id, url, token, program, tuple(args), dict(env), cwd,
                           _duration(data.get("connect_timeout_sec", 10)),
                           _duration(data.get("initialize_timeout_sec", 10)), tuple(required))


def load_environment_snapshot(config_dir: Path, *, environ: Mapping[str, str] | None = None) -> EnvironmentSnapshot:
    path = config_dir / "environments.toml"
    try:
        contents = path.read_bytes()
    except FileNotFoundError:
        value = (os.environ if environ is None else environ).get("CODEX_EXEC_SERVER_URL", "").strip()
        if value.lower() == "none":
            return EnvironmentSnapshot((), False, None)
        if value:
            # Source's env provider defers endpoint failure to connection.
            spec = EnvironmentSpec("remote", url=value)
            return EnvironmentSnapshot((spec,), False, "remote")
        return EnvironmentSnapshot((), True, "local")
    data = tomllib.loads(contents.decode("utf-8"))
    _unknown_fields(data, {"default", "include_local", "environments"}, "environments.toml")
    include_local = data.get("include_local", True)
    if not isinstance(include_local, bool):
        raise ExecutorProtocolError("include_local must be a boolean")
    entries = data.get("environments", [])
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise ExecutorProtocolError("environments must be an array of tables")
    specs = tuple(parse_environment_spec(entry, config_dir) for entry in entries)
    ids = [entry.id for entry in specs]
    if len(set(ids)) != len(ids):
        raise ExecutorProtocolError("environment id is duplicated")
    default = data.get("default", "local" if include_local else "none")
    if not isinstance(default, str) or not default.strip():
        raise ExecutorProtocolError("default environment id cannot be empty")
    default = default.strip()
    if default.lower() == "none":
        default = None
    elif default not in ids and not (include_local and default == "local"):
        raise ExecutorProtocolError(f"default environment `{default}` is not configured")
    return EnvironmentSnapshot(specs, include_local, default)
