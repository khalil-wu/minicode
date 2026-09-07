"""Launch configured live preview dev servers for the active workspace."""
from __future__ import annotations

import asyncio
import codecs
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
MAX_PREVIEW_LOG_LINE_CHARS = 64 * 1024
PREVIEW_OUTPUT_DRAIN_SECONDS = 1.0


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
    _exit_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    @property
    def effective_url(self) -> str:
        return self.detected_url or self.config.url

    @property
    def effective_port(self) -> int:
        return self.detected_port or self.config.port

    @property
    def is_active(self) -> bool:
        return (
            _RUNNING.get(self.id) is self
            and self.process.returncode is None
            and self.status in {"starting", "ready"}
            and not self.cleanup_pending
        )

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
            "cleanup_pending": self.cleanup_pending,
            "cleanup_reason": self.cleanup_reason,
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
        raise PreviewLaunchConfigError(source, f"cwd must stay inside the workspace: {cwd_value}") from None
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

    if find_preview_process(
        url, session_id=session_id, conversation_id=conversation_id, workspace_root=workspace_root,
    ) is not None:
        return True
    try:
        requested = _preview_origin(urlparse(str(url or "").strip()))
    except ValueError:
        return False
    if requested is None:
        return False
    for candidate in extra_urls:
        try:
            candidate_parsed = urlparse(candidate)
            if _preview_origin(candidate_parsed) == requested:
                return True
        except (TypeError, ValueError):
            continue
    return False


def find_preview_process(
    url: str,
    *,
    session_id: str,
    conversation_id: str,
    workspace_root: str | Path | None = None,
) -> PreviewLaunchProcess | None:
    try:
        requested = _preview_origin(urlparse(str(url or "").strip()))
    except ValueError:
        return None
    if requested is None:
        return None
    for process in running_preview_processes(
        session_id=session_id, conversation_id=conversation_id, workspace_root=workspace_root,
    ):
        if not process.is_active:
            continue
        try:
            if _preview_origin(urlparse(process.effective_url)) == requested:
                return process
        except ValueError:
            continue
    return None


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

    async def _record_line(line: str, is_stderr: bool) -> None:
        nonlocal ready_fired
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
            return

        match = OUTPUT_URL_RE.search(line)
        if match:
            url = match.group(0).rstrip(".,;)]}").replace("0.0.0.0", "localhost")
            try:
                parsed = urlparse(url)
                detected_port = parsed.port or (443 if parsed.scheme == "https" else 80)
            except ValueError:
                return
            # ``python -m http.server`` uses an explicit static-file URL;
            # keep that path while learning only the bound port.
            if launched.config.source != "static-html":
                launched.detected_url = url
            launched.detected_port = detected_port
            ready_fired = True
            await mark_preview_ready(launched, broadcast)

    async def _read_stream(stream: asyncio.StreamReader | None, is_stderr: bool) -> None:
        if stream is None:
            return
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = ""
        omitted_characters = 0
        while True:
            raw = await stream.read(4096)
            segments = decoder.decode(raw, final=not raw).split("\n")
            for segment_index, segment in enumerate(segments):
                pending += segment
                overflow = max(0, len(pending) - MAX_PREVIEW_LOG_LINE_CHARS)
                if overflow:
                    pending = pending[overflow:]
                    omitted_characters += overflow
                if segment_index == len(segments) - 1 and raw:
                    continue
                if not raw and not pending and not omitted_characters:
                    continue
                line = pending.rstrip()
                if omitted_characters:
                    line = f"[... {omitted_characters} characters omitted] {line}"
                pending = ""
                omitted_characters = 0
                await _record_line(line, is_stderr)
            if not raw:
                break

    readers = [
        asyncio.create_task(_read_stream(launched.process.stdout, False)),
        asyncio.create_task(_read_stream(launched.process.stderr, True)),
    ]
    output = asyncio.gather(*readers)
    exited = asyncio.create_task(launched._exit_event.wait())
    stopped = asyncio.create_task(launched._stop_event.wait())
    monitor_error = ""
    try:
        try:
            completed, _ = await asyncio.wait((output, exited, stopped), return_when=asyncio.FIRST_COMPLETED)
            if output in completed:
                await output
                await asyncio.wait((exited, stopped), return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            launched._stop_event.set()
        except Exception as exc:
            monitor_error = f"Preview output failed: {exc}"
            logger.warning("Preview monitor error: %s", exc, exc_info=True)
        launched.status = "stopping"
        await _cleanup_preview_process(launched)
        if not output.done():
            completed, _ = await asyncio.wait((output,), timeout=PREVIEW_OUTPUT_DRAIN_SECONDS)
            if not completed:
                monitor_error = monitor_error or "Preview output did not finish after exit; remaining delivery was cancelled"
        if output.done() and not output.cancelled() and output.exception() is not None:
            monitor_error = monitor_error or f"Preview output failed: {output.exception()}"
    finally:
        for task in (*readers, exited, stopped):
            task.cancel()
        await asyncio.gather(*readers, output, exited, stopped, return_exceptions=True)
    was_stopping = launched._stop_event.is_set()
    if monitor_error:
        launched.stderr_tail.append(monitor_error)
    if not launched.cleanup_pending:
        launched.status = "exited" if was_stopping or (launched.process.returncode == 0 and not monitor_error) else "crashed"
    # _RUNNING is keyed by a deterministic preview id, so a restart reuses this
    # exact key. Both awaits above are yield points during which the user may
    # have restarted the preview and overwritten the slot; popping by id alone
    # would evict the live replacement and orphan it beyond every stop path.
    registered = _RUNNING.get(launched.id)
    if registered is launched and not launched.cleanup_pending:
        _RUNNING.pop(launched.id, None)
    elif registered is not None and registered is not launched:
        return

    if broadcast and not was_stopping:
        if launched.cleanup_pending:
            await broadcast({
                "type": "preview.server.unhealthy",
                "id": launched.id,
                "last_error": "Preview resources could not be confirmed stopped; retry stop",
                "cleanup_pending": True,
                "cleanup_reason": launched.cleanup_reason,
                **launched.owner_dict(),
            })
        elif launched.status == "exited":
            await broadcast({"type": "preview.launch.stopped", **launched.to_dict()})
        else:
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
) -> bool:
    """Commit readiness after either process output or HTTP verification."""
    if launched.process.returncode is not None or launched.status not in {"starting", "ready"}:
        return False
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
    return True


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
    if existing is not None:
        if existing.process.returncode is None and existing.status in {"starting", "ready"}:
            return existing
        await _stop_preview_processes([existing])
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
    exit_event = asyncio.Event()
    process = await sandbox_runner.spawn_shell_interactive(
        config.command,
        cwd=config.cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        on_exit=exit_event.set,
    )
    launched = PreviewLaunchProcess(
        id=preview_id,
        config=config,
        process=process,
        session_id=session,
        conversation_id=conversation,
        workspace_root=str(sandbox_root),
        _sandbox_runner=sandbox_runner,
        _exit_event=exit_event,
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
        proc.status = "stopping"
        if proc._monitor_task and not proc._monitor_task.done():
            proc._stop_event.set()
            await proc._monitor_task
        else:
            await _cleanup_preview_process(proc)
        if proc.cleanup_pending:
            unproven.append(proc.id)
            continue
        proc.status = "exited"
        # A restart during the await above may already own this deterministic
        # key; only the entry we actually stopped may leave the registry.
        if _RUNNING.get(proc.id) is proc:
            _RUNNING.pop(proc.id, None)
    if unproven:
        raise RuntimeError(
            "Preview processes could not be proven stopped: " + ", ".join(sorted(unproven))
        )
    return targets


async def _cleanup_preview_process(launched: PreviewLaunchProcess) -> None:
    launched.cleanup_pending = True
    launched.cleanup_reason = "preview_cleanup_pending"
    if launched._sandbox_runner is not None:
        reaped = await launched._sandbox_runner.terminate(launched.process)
    else:
        reaped = await terminate_process_tree(launched.process)
    launched.cleanup_pending = not reaped
    launched.cleanup_reason = "" if reaped else "preview_cleanup_unproven"
    if not reaped:
        launched.status = "unhealthy"
        logger.warning("Preview %s could not be proven stopped; keeping its handle", launched.id)
