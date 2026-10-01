"""Git tool helpers.

Extracted from ``backend/tools/git_tools.py`` so subprocess communication and
path/deny checks are independent of the tool classes.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from dataclasses import replace

from backend.subprocesses import communicate_bounded
from backend.permissions.context import ToolExecutionContext
from backend.sandbox import SandboxPolicy, SandboxRunner
from backend.sandbox.policy import (
    AdditionalPermissionProfile,
    FileSystemAccessMode,
    FileSystemPath,
    FileSystemPermissions,
    FileSystemSandboxEntry,
    SandboxEnforcement,
    sandbox_policy_for_permission_context,
)
from pathlib import Path
from typing import Any
import asyncio


logger = logging.getLogger(__name__)


_GIT_TRANSPORT_LIMIT_BYTES = 20 * 1024 * 1024


async def _run_git(
    argv: list[str],
    *,
    root: Path,
    cwd: Path | None = None,
    context: ToolExecutionContext | None = None,
    sandbox_policy: SandboxPolicy | None = None,
    write_git_metadata: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run repository-selected executables under the captured request authority.

    The tool coroutine remains the owner until spawn, pipes and runner cleanup
    have actually settled. Registry cancellation can retain that same coroutine
    in the shared lifecycle owner; no anonymous cleanup callback replaces it.
    """
    root = root.resolve()
    cwd = cwd or root
    policy = sandbox_policy
    if policy is None and context is not None:
        policy = context.sandbox_policy
        if policy is None:
            policy = sandbox_policy_for_permission_context(root, context.permission)
    if policy is None:
        # Internal readonly snapshots have no implicit host-execution authority.
        policy = SandboxPolicy(workspace_root=root, readable_roots=(root,))
    if timeout is not None:
        policy = replace(policy, timeout=timeout)
    if write_git_metadata:
        policy = await _git_metadata_write_policy(policy, root, context)
    else:
        argv = [argv[0], "--no-optional-locks", *argv[1:]]

    async def execute() -> subprocess.CompletedProcess[bytes]:
        launch_argv, common_dir = _portable_git_dispatch(policy, root, cwd, argv)
        runner = SandboxRunner(policy)
        # Windows sandboxes use PowerShell, including the Linux container's
        # pwsh. Literal single quotes preserve messages/pathspecs as arguments.
        command = (
            "& " + " ".join("'" + value.replace("'", "''") + "'" for value in launch_argv)
            if os.name == "nt" else shlex.join(launch_argv)
        )
        if common_dir is not None:
            # Runtime-owned routing is confined to this child shell, after the
            # runner's normal environment sanitization. No external GIT_* grant.
            command = (
                "$env:GIT_COMMON_DIR='" + common_dir.replace("'", "''") + "'; " + command
                if os.name == "nt" else "GIT_COMMON_DIR=" + shlex.quote(common_dir) + " " + command
            )
        if os.name == "nt" and policy.resolve(cwd=cwd).enforcement in {
            SandboxEnforcement.DISABLED, SandboxEnforcement.EXTERNAL,
        }:
            from backend.tools.command_support import _windows_powershell_shell_command

            command = _windows_powershell_shell_command(command, cwd=cwd)
        try:
            process = await runner.spawn_shell_interactive(
                command, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
            )
            stdout, stderr = await communicate_bounded(
                process, timeout=policy.timeout,
                stdout_limit_bytes=_GIT_TRANSPORT_LIMIT_BYTES,
                stderr_limit_bytes=_GIT_TRANSPORT_LIMIT_BYTES,
            )
            return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
        finally:
            # False is unfinished resource ownership, not a completed receipt.
            # Keep the actual tool child alive until the existing runner reaps it.
            while not await runner.cleanup():
                await asyncio.sleep(0.1)

    _raise_if_cancelled(context)
    operation = asyncio.create_task(execute())
    cancel_event = context.cancel_event if context is not None else None
    cancel_waiter = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
    try:
        if cancel_waiter is None:
            return await asyncio.shield(operation)
        done, _ = await asyncio.wait({operation, cancel_waiter}, return_when=asyncio.FIRST_COMPLETED)
        if cancel_waiter in done:
            raise asyncio.CancelledError
        return operation.result()
    except asyncio.CancelledError:
        operation.cancel()
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                continue
            except Exception:
                break  # The owned operation has settled with an error.
        try:
            operation.result()
        except (asyncio.CancelledError, Exception):
            pass  # Preserve cancellation, after consuming the settled result.
        raise
    finally:
        if cancel_waiter is not None:
            cancel_waiter.cancel()
            await asyncio.gather(cancel_waiter, return_exceptions=True)


def _portable_git_dispatch(
    policy: SandboxPolicy, root: Path, cwd: Path, argv: list[str],
) -> tuple[list[str], str | None]:
    """Translate linked metadata, without translating or granting its authority."""
    mapper = SandboxRunner(policy)
    if mapper.capability(cwd=cwd).backend not in {"docker", "podman"}:
        return argv, None
    resolved = policy.resolve(cwd=cwd)

    def admitted(path: Path) -> Path:
        canonical = path.resolve()
        if not resolved.resolve_access(path).can_read or not resolved.resolve_access(canonical).can_read:
            raise PermissionError(f"Git metadata read denied by captured filesystem policy: {path}")
        return canonical

    worktree = cwd.resolve()
    while worktree.is_relative_to(root):
        marker = admitted(worktree / ".git")
        if marker.is_file():
            value = marker.read_text(encoding="utf-8").strip()
            if not value.startswith("gitdir:") or not value[len("gitdir:"):].strip():
                raise ValueError(f"Invalid Git metadata pointer: {marker}")
            git_dir = admitted(worktree / value[len("gitdir:"):].strip())
            common_file = admitted(git_dir / "commondir")
            common_dir = git_dir
            if common_file.is_file():
                common_dir = admitted(git_dir / common_file.read_text(encoding="utf-8").strip())
            return [
                argv[0],
                f"--git-dir={mapper.map_path_to_sandbox(git_dir)}",
                f"--work-tree={mapper.map_path_to_sandbox(worktree)}",
                *argv[1:],
            ], mapper.map_path_to_sandbox(common_dir)
        if marker.is_dir() or worktree == root:
            break
        worktree = worktree.parent
    return argv, None


async def _git_metadata_write_policy(
    policy: SandboxPolicy, root: Path, context: ToolExecutionContext | None,
) -> SandboxPolicy:
    resolved = policy.resolve(cwd=root)
    if resolved.enforcement in {SandboxEnforcement.DISABLED, SandboxEnforcement.EXTERNAL}:
        return policy
    if not any(writable.is_path_writable(root) for writable in resolved.writable_roots):
        raise PermissionError("Git commit requires workspace-write authority")
    # Discover through the same sandbox, never a host rev-parse. This handles
    # nested repositories and gitfiles without trusting their arbitrary target.
    discovered = await _run_git(
        ["git", "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"],
        root=root, context=context, sandbox_policy=policy,
    )
    if discovered.returncode != 0:
        raise PermissionError(discovered.stderr.decode("utf-8", errors="replace").strip())
    mapper = SandboxRunner(policy)
    entries: list[FileSystemSandboxEntry] = []
    for raw_path in discovered.stdout.decode("utf-8").splitlines():
        metadata = Path(mapper.map_path_from_sandbox(raw_path)).resolve()
        if not metadata.is_relative_to(root):
            if resolved.resolve_access(metadata) is not FileSystemAccessMode.WRITE or not any(
                writable.is_path_writable(metadata) for writable in resolved.writable_roots
            ):
                raise PermissionError("Git metadata outside the workspace needs explicit write authority")
            continue  # Use an existing precise grant; never widen it from a gitfile.
        if resolved.resolve_access(metadata) is FileSystemAccessMode.DENY:
            raise PermissionError("Git metadata is denied by the captured filesystem policy")
        entries.append(FileSystemSandboxEntry(FileSystemPath.path(metadata), FileSystemAccessMode.WRITE))
    return policy.with_additional_permissions(
        AdditionalPermissionProfile(file_system=FileSystemPermissions(tuple(entries)))
    )


async def _communicate_git(
    proc: asyncio.subprocess.Process,
) -> tuple[bytes, bytes]:
    return await communicate_bounded(
        proc,
        stdout_limit_bytes=_GIT_TRANSPORT_LIMIT_BYTES,
        stderr_limit_bytes=_GIT_TRANSPORT_LIMIT_BYTES,
    )


def _raise_if_cancelled(context: Any) -> None:
    cancel_event = getattr(context, "cancel_event", None) if context is not None else None
    if cancel_event is not None and cancel_event.is_set():
        raise asyncio.CancelledError


def _workspace_root(context: Any, fallback: Path | None) -> Path | None:
    if context is not None and hasattr(context, "workspace_root"):
        return Path(context.workspace_root).resolve() if context.workspace_root is not None else None
    return fallback.resolve() if fallback is not None else None


def _resolve_work_dir(root: Path, path_value: Any) -> Path:
    raw = str(path_value or ".").strip() or "."
    resolved = (root / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
    # Confine to the workspace: an absolute or ../-escaping path must not let a
    # read-only AUTO tool inspect arbitrary repos on the host. Escapes fall back
    # to the workspace root rather than running git in an unrelated directory.
    root_resolved = root.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        return root_resolved
    return resolved


def _is_denied_path(context: Any, file_path: str) -> bool:
    checker = getattr(context, "permission_checker", None) if context is not None else None
    if checker is None:
        return False
    permission = getattr(context, "permission", None)
    try:
        return not checker.is_path_allowed(str(file_path), context=permission)
    except Exception:
        # A read-only diff must not become an information-disclosure path when
        # the policy boundary itself fails.  Callers treat ``True`` as denied,
        # so a focused diff is rejected and a broad diff omits the path.
        logger.warning(
            "Git diff permission check failed closed for %s",
            file_path,
            exc_info=True,
        )
        return True


