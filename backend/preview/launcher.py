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

from backend.sandbox import (
    AdditionalPermissionProfile,
    FileSystemAccessMode,
    FileSystemPath,
    FileSystemPermissions,
    FileSystemSandboxEntry,
    NetworkPermissions,
    SandboxPolicy,
    SandboxRunner,
)
from backend.subprocesses import terminate_process_tree

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
    # Set when a stop request could not prove the preview tree exited. The
    # process then stays registered so its exit can still be observed.
    cleanup_pending: bool = False
    cleanup_reason: str = ""
    stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=20))
    output_tail: deque[dict[str, str]] = field(default_factory=lambda: deque(maxlen=80))
    _monitor_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _sandbox_runner: SandboxRunner | None = field(default=None, repr=False)
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


class PreviewLaunchConfigError(RuntimeError):
    """The user's explicit preview configuration could not be honoured.

    A misconfigured ``.minicode/launch.json`` must never be substituted by an
    inferred ``package.json`` guess: the user asked for something specific and
    has to be told their file was rejected, not silently overruled.
    """

    def __init__(self, source: str, reason: str):
        self.source = source
        self.reason = reason
        super().__init__(f"{source} is invalid: {reason}")


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
    """Return the preview configurations for a workspace.

    ``.minicode/launch.json`` is the user's explicit declaration: if it exists it
    is authoritative, and anything wrong with it raises
    :class:`PreviewLaunchConfigError` instead of degrading into "this project has
    no preview configuration" and silently inferring an ``npm run dev`` guess
    from ``package.json``.  The ``package.json`` inference only runs when no
    ``launch.json`` is present at all.
    """

    root = _safe_workspace_root(workspace_root)
    launch_path = root / ".minicode" / "launch.json"
    if launch_path.exists():
        source = ".minicode/launch.json"
        try:
            payload = json.loads(launch_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise PreviewLaunchConfigError(source, f"the file could not be read ({exc})") from exc
        except json.JSONDecodeError as exc:
            raise PreviewLaunchConfigError(source, f"invalid JSON at line {exc.lineno} ({exc.msg})") from exc
        if not isinstance(payload, dict):
            raise PreviewLaunchConfigError(source, f"top-level value is {type(payload).__name__}, expected an object")
        raw_configs = payload.get("configurations")
        if not isinstance(raw_configs, list):
            raise PreviewLaunchConfigError(source, '"configurations" must be a list')
        configs: list[PreviewLaunchConfig] = []
        for index, item in enumerate(raw_configs):
            if not isinstance(item, dict):
                raise PreviewLaunchConfigError(
                    source, f"configurations[{index}] is {type(item).__name__}, expected an object"
                )
            config = _coerce_config(item, root, source)
            if config is None:
                raise PreviewLaunchConfigError(
                    source,
                    f'configurations[{index}] needs a non-empty "command" or "runtimeExecutable" string',
                )
            configs.append(config)
        return configs

    package_json = root / "package.json"
    if package_json.exists():
        try:
            payload = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # package.json is an inference source, not the user's preview
            # declaration, so a broken one leaves evidence without blocking the
            # panel. .minicode/launch.json is the channel for an explicit choice.
            logger.warning("Cannot infer a preview command from %s: %s", package_json, exc)
            return []
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


def preview_url_is_owned(
    url: str,
    *,
    session_id: str = "",
    conversation_id: str = "",
    workspace_root: str | Path | None = None,
    extra_urls: tuple[str, ...] | list[str] = (),
) -> bool:
    """Check a URL against the origin of an active, owner-scoped preview.

    The check intentionally compares origins rather than paths: a preview
    server may serve a router entry point, assets, and API endpoints on the
    same bound port.  Ownership remains exact on session, conversation, and
    workspace, so another conversation cannot borrow a local port merely by
    knowing its number.
    """

    try:
        parsed = urlparse(str(url or "").strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return False
        requested = _preview_origin(parsed)
    except (TypeError, ValueError):
        return False

    candidates = [str(value).strip() for value in extra_urls if str(value).strip()]
    candidates.extend(
        process.effective_url
        for process in running_preview_processes(
            session_id=session_id,
            conversation_id=conversation_id,
            workspace_root=workspace_root,
        )
        if process.effective_url
    )
    for candidate in candidates:
        try:
            candidate_parsed = urlparse(candidate)
            if _preview_origin(candidate_parsed) == requested:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _preview_origin(parsed: Any) -> tuple[str, str, int] | None:
    scheme = str(getattr(parsed, "scheme", "") or "").lower()
    host = str(getattr(parsed, "hostname", "") or "").lower().rstrip(".")
    if scheme not in {"http", "https"} or not host:
        return None
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        return None
    return scheme, host, int(port)


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
    if launched._sandbox_runner is not None:
        # Long-lived container sandboxes keep a cidfile/name owned by the
        # runner. Natural exit must release that state just like explicit stop.
        if not await launched._sandbox_runner.terminate(launched.process):
            logger.warning(
                "Preview %s exited but its sandbox resources could not be proven released",
                launched.id,
            )
    was_stopping = launched.status == "stopping"
    launched.status = "exited" if was_stopping or launched.process.returncode == 0 else "crashed"
    # _RUNNING is keyed by a deterministic preview id, so a restart reuses this
    # exact key. Both awaits above are yield points during which the user may
    # have restarted the preview and overwritten the slot; popping by id alone
    # would evict the live replacement and orphan it beyond every stop path.
    if _RUNNING.get(launched.id) is launched:
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
    sandbox_policy: SandboxPolicy | None = None,
) -> PreviewLaunchProcess:
    session = str(session_id or "").strip()
    conversation = str(conversation_id or "").strip()
    if not session or not conversation:
        raise RuntimeError("Preview launch requires a session and conversation owner")
    configs = load_preview_launch_configs(workspace_root)
    if not configs:
        raise RuntimeError("No preview launch configuration found")
    requested = str(name or "").strip()
    if not requested:
        config = configs[0]
    else:
        config = next((item for item in configs if item.name == requested), None)
        if config is None:
            # Falling back to configs[0] started a different server than asked
            # for and reported success with its URL — the caller (including the
            # model's preview tool) had no way to see the substitution.
            raise RuntimeError(
                f"No preview configuration named '{requested}'. Available: "
                + ", ".join(item.name for item in configs)
            )
    return await _start_preview_config(
        config,
        broadcast,
        session_id=session,
        conversation_id=conversation,
        workspace_root=_safe_workspace_root(workspace_root),
        sandbox_policy=sandbox_policy,
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
    sandbox_policy: SandboxPolicy | None = None,
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
    env_overrides: dict[str, str] = {}
    if config.port:
        env_overrides["PORT"] = str(config.port)
    sandbox_root = _safe_workspace_root(workspace_root or config.cwd)
    runtime_readable_roots = _preview_runtime_readable_roots(
        include_code_root=config.source == "static-html"
    )
    if sandbox_policy is None:
        # Desktop preview commands are explicit user control-plane operations.
        policy = SandboxPolicy(
            workspace_root=sandbox_root,
            writable_roots=(sandbox_root,),
            readable_roots=runtime_readable_roots,
            allow_network=True,
            env_overrides=env_overrides,
        )
    else:
        if sandbox_policy.policy_limitations:
            raise RuntimeError(
                "Preview launch is blocked because the managed network policy "
                "requires enforcement that is unavailable on this host: "
                + "; ".join(sandbox_policy.policy_limitations)
            )
        # Starting preview_server is a CONFIRM tool action. Represent that
        # approval as an additional network capability while preserving every
        # filesystem deny, writable-root and fail-closed setting from the turn.
        resolved_policy = sandbox_policy.resolve()
        runtime_read_entries = tuple(
            FileSystemSandboxEntry(
                FileSystemPath.path(root),
                FileSystemAccessMode.READ,
            )
            for root in runtime_readable_roots
            if resolved_policy.resolve_access(root) is FileSystemAccessMode.DENY
        )
        policy = sandbox_policy.with_additional_permissions(
            AdditionalPermissionProfile(
                file_system=(
                    FileSystemPermissions(entries=runtime_read_entries)
                    if runtime_read_entries
                    else None
                ),
                network=NetworkPermissions(enabled=True),
            )
        )
        policy = replace(
            policy,
            env_overrides={**policy.env_overrides, **env_overrides},
            timeout=None,
        )
    sandbox_runner = SandboxRunner(policy)
    process = await sandbox_runner.spawn_shell_interactive(
        config.command,
        cwd=config.cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    launched = PreviewLaunchProcess(
        id=preview_id,
        config=config,
        process=process,
        session_id=session,
        conversation_id=conversation,
        workspace_root=str(sandbox_root),
        _sandbox_runner=sandbox_runner,
    )
    _RUNNING[preview_id] = launched
    launched._monitor_task = asyncio.create_task(_monitor_process(launched, broadcast))
    return launched


def _allocate_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _preview_runtime_readable_roots(
    *, include_code_root: bool,
) -> tuple[Path, ...]:
    """Return the interpreter roots needed by the preview command."""

    roots: list[Path] = []
    if include_code_root:
        roots.append(Path(__file__).resolve().parents[2])
    prefixes = [Path(value).resolve() for value in (sys.prefix, sys.base_prefix)]
    roots.extend(prefixes)
    if prefixes:
        # Virtualenv interpreters commonly symlink through a versioned Python
        # installation. Keep the symlink directory visible without exposing
        # the user's whole home directory.
        roots.append(prefixes[-1].parent)
    return tuple(dict.fromkeys(root for root in roots if root.exists()))


async def start_static_preview(
    workspace_root: str | Path | None,
    file_path: str | Path,
    broadcast: BroadcastFn | None = None,
    *,
    session_id: str,
    conversation_id: str,
    sandbox_policy: SandboxPolicy | None = None,
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
        sandbox_policy=sandbox_policy,
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


async def stop_preview_launches_for_conversation(conversation_id: str) -> list[PreviewLaunchProcess]:
    """Stop every preview writer owned by a deleted conversation."""

    owner = str(conversation_id or "").strip()
    if not owner:
        return []
    return await _stop_preview_processes([
        process
        for process in _active_preview_processes()
        if process.conversation_id == owner
    ])


async def stop_all_preview_launches() -> list[PreviewLaunchProcess]:
    """Stop all previews during application shutdown."""
    return await _stop_preview_processes(_active_preview_processes())


async def _stop_preview_processes(
    targets: list[PreviewLaunchProcess],
) -> list[PreviewLaunchProcess]:
    """Stop previews and refuse to report a teardown that was not proven.

    A surviving dev server keeps writing to the workspace, so its registry
    entry and monitor task are retained as the reaping handle and the caller
    is told the cleanup is unfinished.
    """
    unproven: list[str] = []
    for proc in targets:
        if proc.process.returncode is None:
            proc.status = "stopping"
            if proc._sandbox_runner is not None:
                reaped = await proc._sandbox_runner.terminate(proc.process)
            else:
                reaped = await terminate_process_tree(proc.process)
            if not reaped:
                proc.cleanup_pending = True
                proc.cleanup_reason = "preview_tree_survived_kill"
                unproven.append(proc.id)
                logger.warning(
                    "Preview %s could not be proven stopped; keeping its handle",
                    proc.id,
                )
                continue
        proc.cleanup_pending = False
        proc.cleanup_reason = ""
        if proc._monitor_task and not proc._monitor_task.done():
            proc._monitor_task.cancel()
        # A restart during the await above may already own this deterministic
        # key; only the entry we actually stopped may leave the registry.
        if _RUNNING.get(proc.id) is proc:
            _RUNNING.pop(proc.id, None)
    if unproven:
        raise RuntimeError(
            "Preview processes could not be proven stopped: " + ", ".join(sorted(unproven))
        )
    return targets
