"""Launch configured live preview dev servers for the active workspace."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import shlex
import socket
import subprocess
import sys
from collections import deque
from contextlib import suppress
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Coroutine
from urllib.parse import quote, urlparse

from backend.runtime_env import sanitized_subprocess_env
from backend.subprocesses import spawn_shell, terminate_process_tree

logger = logging.getLogger(__name__)

OUTPUT_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


@dataclass(frozen=True)
class PreviewLaunchConfig:
    name: str
    command: str
    cwd: str
    port: int = 0
    url: str = ""
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
    session_id: str = ""
    conversation_id: str = ""
    workspace_root: str = ""
    stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=20))
    output_tail: deque[dict[str, str]] = field(default_factory=lambda: deque(maxlen=80))
    _monitor_task: asyncio.Task[None] | None = field(default=None, repr=False)
    ready_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

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
            "session_id": self.session_id,
            "conversation_id": self.conversation_id,
            "workspace_root": self.workspace_root,
        }

    def owner_dict(self) -> dict[str, str]:
        return {
            "session_id": self.session_id,
            "conversation_id": self.conversation_id,
            "workspace_root": self.workspace_root,
        }


BroadcastFn = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]

_RUNNING: dict[str, PreviewLaunchProcess] = {}


def _active_preview_processes() -> list[PreviewLaunchProcess]:
    stopped = [key for key, proc in _RUNNING.items() if proc.process.returncode is not None]
    for key in stopped:
        proc = _RUNNING.pop(key)
        proc.status = "exited"
    return list(_RUNNING.values())


def all_running_preview_processes() -> list[PreviewLaunchProcess]:
    """Return the process-global snapshot for internal health diagnostics only."""
    return _active_preview_processes()


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
    if not isinstance(port, int) or not (1 <= port <= 65535):
        port = 0
    url = raw.get("url")
    if not isinstance(url, str):
        url = ""
    url = url.strip()
    if url and not port:
        try:
            parsed_port = urlparse(url).port
        except ValueError:
            parsed_port = None
        if parsed_port:
            port = parsed_port
    if not url and port:
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
                        script = str(scripts[script_name])
                        is_vite = bool(re.search(r"(?:^|\s|[/\\])vite(?:\s|$)", script))
                        port = 5173 if is_vite else 0
                        return [
                            PreviewLaunchConfig(
                                name=f"npm run {script_name}",
                                command=f"npm run {script_name}",
                                cwd=str(root),
                                port=port,
                                url=f"http://127.0.0.1:{port}" if port else "",
                                source="package.json",
                            )
                        ]
        except (OSError, json.JSONDecodeError):
            pass

    return []


def running_preview_processes(
    *,
    session_id: str,
    conversation_id: str,
    workspace_root: str | Path | None = None,
) -> list[PreviewLaunchProcess]:
    session = str(session_id or "").strip()
    conversation = str(conversation_id or "").strip()
    if not session or not conversation:
        return []
    workspace = str(Path(workspace_root).resolve()) if workspace_root else ""
    return [
        process
        for process in _active_preview_processes()
        if process.session_id == session
        and process.conversation_id == conversation
        and (not workspace or process.workspace_root == workspace)
    ]


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
                    **launched.owner_dict(),
                })

            if ready_fired:
                continue

            match = OUTPUT_URL_RE.search(line)
            if match:
                url = match.group(0).rstrip(".,;)]}").replace("0.0.0.0", "localhost")
                try:
                    parsed = urlparse(url)
                    detected_port = parsed.port or (443 if parsed.scheme == "https" else 80)
                except ValueError:
                    continue
                # ``python -m http.server`` uses an explicit static-file URL;
                # keep that path while learning only the bound port.
                if launched.config.source != "static-html":
                    launched.detected_url = url
                launched.detected_port = detected_port
                ready_fired = True
                await mark_preview_ready(launched, broadcast)

    try:
        await asyncio.gather(
            _read_stream(launched.process.stdout, False),
            _read_stream(launched.process.stderr, True),
        )
    except Exception as exc:
        logger.debug("Preview monitor error: %s", exc)

    await launched.process.wait()
    was_stopping = launched.status == "stopping"
    launched.status = "exited" if was_stopping or launched.process.returncode == 0 else "crashed"
    _RUNNING.pop(launched.id, None)

    if broadcast and not was_stopping and launched.process.returncode != 0:
        await broadcast({
            "type": "preview.server.crashed",
            "id": launched.id,
            "exit_code": launched.process.returncode,
            "stderr_tail": list(launched.stderr_tail),
            **launched.owner_dict(),
        })


async def start_preview_launch(
    workspace_root: str | Path | None,
    name: str | None = None,
    broadcast: BroadcastFn | None = None,
    *,
    session_id: str,
    conversation_id: str,
) -> PreviewLaunchProcess:
    session = str(session_id or "").strip()
    conversation = str(conversation_id or "").strip()
    if not session or not conversation:
        raise RuntimeError("Preview launch requires a session and conversation owner")
    configs = load_preview_launch_configs(workspace_root)
    if not configs:
        raise RuntimeError("No preview launch configuration found")
    config = next((item for item in configs if item.name == name), configs[0])
    return await _start_preview_config(
        config,
        broadcast,
        session_id=session,
        conversation_id=conversation,
        workspace_root=_safe_workspace_root(workspace_root),
    )


async def mark_preview_ready(
    launched: PreviewLaunchProcess,
    broadcast: BroadcastFn | None = None,
) -> None:
    """Commit readiness after either process output or HTTP verification."""
    transitioned = launched.status != "ready"
    launched.status = "ready"
    launched.ready_event.set()
    if transitioned and broadcast:
        await broadcast({
            "type": "preview.server.ready",
            "id": launched.id,
            "url": launched.effective_url,
            "port": launched.effective_port,
            **launched.owner_dict(),
        })


async def _start_preview_config(
    config: PreviewLaunchConfig,
    broadcast: BroadcastFn | None = None,
    *,
    session_id: str,
    conversation_id: str,
    workspace_root: str | Path | None = None,
) -> PreviewLaunchProcess:
    session = str(session_id or "").strip()
    conversation = str(conversation_id or "").strip()
    if not session or not conversation:
        raise RuntimeError("Preview launch requires a session and conversation owner")
    preview_id = hashlib.sha256(
        f"{session}\0{conversation}\0{Path(config.cwd).resolve()}\0{config.name}".encode("utf-8")
    ).hexdigest()
    existing = _RUNNING.get(preview_id)
    if existing and existing.process.returncode is None:
        return existing
    if config.auto_port and not config.port:
        port = _allocate_loopback_port()
        config = replace(
            config,
            port=port,
            url=config.url or f"http://127.0.0.1:{port}",
        )
    env = sanitized_subprocess_env()
    if config.port:
        env.setdefault("PORT", str(config.port))
    process = await spawn_shell(
        config.command,
        cwd=config.cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    launched = PreviewLaunchProcess(
        id=preview_id,
        config=config,
        process=process,
        session_id=session,
        conversation_id=conversation,
        workspace_root=str(_safe_workspace_root(workspace_root or config.cwd)),
    )
    _RUNNING[preview_id] = launched
    launched._monitor_task = asyncio.create_task(_monitor_process(launched, broadcast))
    return launched


def _allocate_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def start_static_preview(
    workspace_root: str | Path | None,
    file_path: str | Path,
    broadcast: BroadcastFn | None = None,
    *,
    session_id: str,
    conversation_id: str,
) -> PreviewLaunchProcess:
    root = _safe_workspace_root(workspace_root)
    target = Path(file_path)
    target = target.resolve() if target.is_absolute() else (root / target).resolve()
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("Static preview file must be inside the active workspace") from exc
    if not target.is_file():
        raise RuntimeError(f"Static preview file does not exist: {relative.as_posix()}")
    if target.suffix.lower() not in {".html", ".htm"}:
        raise RuntimeError("Static preview path must be an HTML file")

    port = _allocate_loopback_port()
    access_token = secrets.token_urlsafe(24)
    argv = [
        sys.executable,
        "-m",
        "backend.preview.static_server",
        "--port",
        str(port),
        "--root",
        str(target.parent),
        "--token",
        access_token,
    ]
    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    identity = hashlib.sha256(str(target).encode("utf-8")).hexdigest()
    config = PreviewLaunchConfig(
        name=f"static-{identity}",
        command=command,
        cwd=str(Path(__file__).resolve().parents[2]),
        port=port,
        url=f"http://127.0.0.1:{port}/{access_token}/{quote(target.name)}",
        source="static-html",
    )
    return await _start_preview_config(
        config,
        broadcast,
        session_id=session_id,
        conversation_id=conversation_id,
        workspace_root=root,
    )


async def stop_preview_launch(
    name: str | None = None,
    *,
    session_id: str,
    conversation_id: str,
    workspace_root: str | Path | None = None,
) -> list[PreviewLaunchProcess]:
    targets = [
        proc for proc in running_preview_processes(
            session_id=session_id,
            conversation_id=conversation_id,
            workspace_root=workspace_root,
        )
        if name is None or proc.config.name == name or proc.id == name
    ]
    return await _stop_preview_processes(targets)


async def stop_preview_launches_for_session(session_id: str) -> list[PreviewLaunchProcess]:
    """Stop every preview owned by one websocket session during shutdown."""
    session = str(session_id or "").strip()
    if not session:
        return []
    return await _stop_preview_processes([
        process
        for process in _active_preview_processes()
        if process.session_id == session
    ])


async def stop_all_preview_launches() -> list[PreviewLaunchProcess]:
    """Stop all previews during application shutdown."""
    return await _stop_preview_processes(_active_preview_processes())


async def _stop_preview_processes(
    targets: list[PreviewLaunchProcess],
) -> list[PreviewLaunchProcess]:
    for proc in targets:
        if proc.process.returncode is None:
            proc.status = "stopping"
            await terminate_process_tree(proc.process)
        if proc._monitor_task and not proc._monitor_task.done():
            proc._monitor_task.cancel()
        _RUNNING.pop(proc.id, None)
    return targets
