"""Launch configured live preview dev servers for the active workspace."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine

from backend.runtime_env import sanitized_subprocess_env

logger = logging.getLogger(__name__)

READY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"Local:\s*(https?://\S+)", re.IGNORECASE),
    re.compile(r"ready\s+(?:on|at|in)\s*(https?://\S+)", re.IGNORECASE),
    re.compile(r"listening\s+(?:on|at)\s*(https?://\S+)", re.IGNORECASE),
    re.compile(r"started\s+(?:server\s+)?(?:on|at)\s*(https?://\S+)", re.IGNORECASE),
    re.compile(r"available\s+(?:on|at)\s*(https?://\S+)", re.IGNORECASE),
    re.compile(r"(https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0):\d+)", re.IGNORECASE),
]

PORT_RE = re.compile(r":(\d{2,5})(?:[/\s]|$)")


@dataclass(frozen=True)
class PreviewLaunchConfig:
    name: str
    command: str
    cwd: str
    port: int
    url: str
    auto_port: bool = False
    source: str = "inferred"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PreviewLaunchProcess:
    id: str
    config: PreviewLaunchConfig
    process: asyncio.subprocess.Process
    status: str = "starting"
    detected_url: str = ""
    detected_port: int = 0
    stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=20))
    output_tail: deque[dict[str, str]] = field(default_factory=lambda: deque(maxlen=80))
    _monitor_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _health_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _health_failures: int = field(default=0, repr=False)

    @property
    def effective_url(self) -> str:
        return self.detected_url or self.config.url

    @property
    def effective_port(self) -> int:
        return self.detected_port or self.config.port

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.config.name,
            "command": self.config.command,
            "cwd": self.config.cwd,
            "port": self.effective_port,
            "url": self.effective_url,
            "pid": self.process.pid,
            "status": self.status,
            "stderr_tail": list(self.stderr_tail),
            "output_tail": list(self.output_tail),
        }


BroadcastFn = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]

_RUNNING: dict[str, PreviewLaunchProcess] = {}


def _safe_workspace_root(root: str | Path | None) -> Path:
    return Path(root).resolve() if root else Path.cwd().resolve()


def _coerce_config(raw: dict[str, Any], workspace_root: Path, source: str) -> PreviewLaunchConfig | None:
    command = raw.get("command") or raw.get("runtimeExecutable")
    if not isinstance(command, str) or not command.strip():
        return None
    args = raw.get("args") or raw.get("runtimeArgs")
    if isinstance(args, list) and args:
        command = " ".join([command, *[str(arg) for arg in args]])
    cwd_value = raw.get("cwd") if isinstance(raw.get("cwd"), str) else "."
    cwd = (workspace_root / cwd_value).resolve()
    try:
        cwd.relative_to(workspace_root)
    except ValueError:
        cwd = workspace_root
    port = raw.get("port")
    if not isinstance(port, int):
        port = 5173
    url = raw.get("url")
    if not isinstance(url, str) or not url.strip():
        url = f"http://127.0.0.1:{port}"
    name = raw.get("name") if isinstance(raw.get("name"), str) else "Dev Server"
    return PreviewLaunchConfig(
        name=name,
        command=command.strip(),
        cwd=str(cwd),
        port=port,
        url=url.strip(),
        auto_port=bool(raw.get("autoPort") or raw.get("auto_port")),
        source=source,
    )


def load_preview_launch_configs(workspace_root: str | Path | None) -> list[PreviewLaunchConfig]:
    root = _safe_workspace_root(workspace_root)
    configs: list[PreviewLaunchConfig] = []
    launch_path = root / ".claude" / "launch.json"
    if launch_path.exists():
        try:
            payload = json.loads(launch_path.read_text(encoding="utf-8"))
            raw_configs = payload.get("configurations") if isinstance(payload, dict) else None
            if isinstance(raw_configs, list):
                configs.extend(
                    config
                    for item in raw_configs
                    if isinstance(item, dict)
                    for config in [_coerce_config(item, root, ".claude/launch.json")]
                    if config is not None
                )
        except (OSError, json.JSONDecodeError):
            pass

    if configs:
        return configs

    package_json = root / "package.json"
    if package_json.exists():
        try:
            payload = json.loads(package_json.read_text(encoding="utf-8"))
            scripts = payload.get("scripts") if isinstance(payload, dict) else None
            if isinstance(scripts, dict):
                for script_name in ("dev", "start", "serve"):
                    if isinstance(scripts.get(script_name), str):
                        port = 3000 if script_name == "start" else 5173
                        return [
                            PreviewLaunchConfig(
                                name=f"npm run {script_name}",
                                command=f"npm run {script_name}",
                                cwd=str(root),
                                port=port,
                                url=f"http://127.0.0.1:{port}",
                                source="package.json",
                            )
                        ]
        except (OSError, json.JSONDecodeError):
            pass

    return []


def running_preview_processes() -> list[PreviewLaunchProcess]:
    stopped = [key for key, proc in _RUNNING.items() if proc.process.returncode is not None]
    for key in stopped:
        proc = _RUNNING.pop(key)
        proc.status = "exited"
    return list(_RUNNING.values())


async def _monitor_process(
    launched: PreviewLaunchProcess,
    broadcast: BroadcastFn | None,
) -> None:
    """Read stdout/stderr, detect ready URL, and report crashes."""
    ready_fired = False

    async def _read_stream(stream: asyncio.StreamReader | None, is_stderr: bool) -> None:
        nonlocal ready_fired
        if stream is None:
            return
        while True:
            raw = await stream.readline()
            if not raw:
                break
            try:
                line = raw.decode("utf-8", errors="replace").rstrip()
            except Exception:
                continue

            if is_stderr:
                launched.stderr_tail.append(line)
            stream_name = "stderr" if is_stderr else "stdout"
            launched.output_tail.append({"stream": stream_name, "line": line})
            if broadcast:
                await broadcast({
                    "type": "preview.server.output",
                    "id": launched.id,
                    "stream": stream_name,
                    "line": line,
                })

            if ready_fired:
                continue

            for pattern in READY_PATTERNS:
                m = pattern.search(line)
                if m:
                    url = m.group(1).replace("0.0.0.0", "localhost")
                    launched.detected_url = url
                    port_match = PORT_RE.search(url)
                    if port_match:
                        launched.detected_port = int(port_match.group(1))
                    launched.status = "ready"
                    ready_fired = True
                    if broadcast:
                        await broadcast({
                            "type": "preview.server.ready",
                            "id": launched.id,
                            "url": launched.effective_url,
                            "port": launched.effective_port,
                        })
                    launched._health_task = asyncio.create_task(
                        _health_check_loop(launched, broadcast)
                    )
                    break

    try:
        await asyncio.gather(
            _read_stream(launched.process.stdout, False),
            _read_stream(launched.process.stderr, True),
        )
    except Exception as exc:
        logger.debug("Preview monitor error: %s", exc)

    await launched.process.wait()
    launched.status = "crashed" if launched.process.returncode != 0 else "exited"
    _RUNNING.pop(launched.id, None)

    if broadcast:
        await broadcast({
            "type": "preview.server.crashed",
            "id": launched.id,
            "exit_code": launched.process.returncode,
            "stderr_tail": list(launched.stderr_tail),
        })


async def start_preview_launch(
    workspace_root: str | Path | None,
    name: str | None = None,
    broadcast: BroadcastFn | None = None,
) -> PreviewLaunchProcess:
    configs = load_preview_launch_configs(workspace_root)
    if not configs:
        raise RuntimeError("No preview launch configuration found")
    config = next((item for item in configs if item.name == name), configs[0])
    existing = _RUNNING.get(config.name)
    if existing and existing.process.returncode is None:
        return existing
    env = sanitized_subprocess_env()
    env.setdefault("PORT", str(config.port))
    process = await asyncio.create_subprocess_shell(
        config.command,
        cwd=config.cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **({"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}),
    )
    launched = PreviewLaunchProcess(id=config.name, config=config, process=process)
    _RUNNING[config.name] = launched
    launched._monitor_task = asyncio.create_task(_monitor_process(launched, broadcast))
    return launched


async def stop_preview_launch(name: str | None = None) -> list[PreviewLaunchProcess]:
    targets = [
        proc for proc in running_preview_processes()
        if name is None or proc.config.name == name or proc.id == name
    ]
    for proc in targets:
        if proc.process.returncode is None:
            proc.process.terminate()
            try:
                await asyncio.wait_for(proc.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.process.kill()
                await proc.process.wait()
        if proc._monitor_task and not proc._monitor_task.done():
            proc._monitor_task.cancel()
        if proc._health_task and not proc._health_task.done():
            proc._health_task.cancel()
        _RUNNING.pop(proc.id, None)
    return targets


HEALTH_CHECK_INTERVAL = 15.0
HEALTH_FAILURE_THRESHOLD = 3


async def _health_check_loop(
    launched: PreviewLaunchProcess,
    broadcast: BroadcastFn | None,
) -> None:
    """Periodically verify the server is still responding."""
    from backend.preview.verifier import verify_preview_url

    while launched.process.returncode is None and launched.id in _RUNNING:
        await asyncio.sleep(HEALTH_CHECK_INTERVAL)
        if launched.process.returncode is not None:
            break
        url = launched.effective_url
        if not url:
            continue
        result = await verify_preview_url(url, timeout=5.0)
        if result.ok:
            launched._health_failures = 0
            if launched.status == "unhealthy":
                launched.status = "ready"
                if broadcast:
                    await broadcast({
                        "type": "preview.server.ready",
                        "id": launched.id,
                        "url": launched.effective_url,
                        "port": launched.effective_port,
                    })
        else:
            launched._health_failures += 1
            if launched._health_failures >= HEALTH_FAILURE_THRESHOLD and launched.status != "unhealthy":
                launched.status = "unhealthy"
                if broadcast:
                    await broadcast({
                        "type": "preview.server.unhealthy",
                        "id": launched.id,
                        "url": launched.effective_url,
                        "consecutive_failures": launched._health_failures,
                        "last_error": result.error,
                    })
