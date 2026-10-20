"""Run configured project lifecycle scripts through the existing shell sandbox."""
from __future__ import annotations

import asyncio
import logging
import json
import os
import shlex
import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from backend.services.local_environment_settings import (
    LifecycleScript, bind_worktree_environment, effective_environment, source_checkout_root, store_worktree_shell_environment, worktree_shell_policy, worktree_source_workspace,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EnvironmentScriptResult:
    stdout: str
    stderr: str
    exit_code: int | None


class EnvironmentScriptError(RuntimeError):
    def __init__(self, message: str, *, cleanup_pending: bool = False):
        super().__init__(message)
        self.cleanup_pending = cleanup_pending


async def execute_environment_script(script: str, cwd: Path, variables: dict[str, str], *, context=None, source_root: Path | None = None, capture_directory: Path | None = None, application_config=None) -> EnvironmentScriptResult:
    from backend.sandbox.policy import (AdditionalPermissionProfile, FileSystemAccessMode,
        FileSystemPath, FileSystemPermissions, FileSystemSandboxEntry, sandbox_policy_for_permission_context)
    from backend.sandbox.runner import SandboxRunner
    from backend.permissions.context import PermissionContext
    from backend.workspace.executor_service import uses_executor_io

    write_paths = [cwd, *([capture_directory] if capture_directory is not None else [])]
    if context is not None and uses_executor_io(context):
        from backend.workspace.executor_editor import ExecutorEditorService
        from backend.workspace.executor_commands import run_workbench_command
        from backend.permissions.environment_command import executor_shell_argv

        async with ExecutorEditorService(context, application_config=application_config or context.model_execution.config) as service:
            if not service.scope.resolve_access(service.cwd.uri).can_write:
                raise PermissionError("Local environment scripts require workspace write permission.")
            service.cwd = service.resolve(cwd.as_uri())
            service.scope = service.scope.with_additional_permissions(AdditionalPermissionProfile(file_system=FileSystemPermissions((
                *(FileSystemSandboxEntry(FileSystemPath("path", path.as_uri()), FileSystemAccessMode.WRITE) for path in write_paths),
            ))))
            argv = executor_shell_argv(service.context.turn_environment, script)
            owners = []
            try:
                result = await run_workbench_command(service, argv, timeout=None, env=variables, allow_failure=True, on_runner=owners.append)
            except BaseException as error:
                if owners and not owners[0].closed: error.cleanup_pending = True
                raise
            return EnvironmentScriptResult(result.stdout, result.stderr, result.exit_code)
    permission = context.permission if context is not None else PermissionContext(workspace_root=source_root or cwd)
    policy = context.sandbox_policy if context is not None and context.sandbox_policy is not None else sandbox_policy_for_permission_context(source_root or cwd, permission)
    if not policy.resolve(cwd=source_root or cwd).resolve_access(source_root or cwd).can_write:
        raise PermissionError("Local environment scripts require workspace write permission.")
    policy = policy.with_additional_permissions(AdditionalPermissionProfile(file_system=FileSystemPermissions((
        *(FileSystemSandboxEntry(FileSystemPath.path(path), FileSystemAccessMode.WRITE) for path in write_paths),
    ))))
    policy = replace(policy, shell_environment_policy=worktree_shell_policy(policy.shell_environment_policy, cwd),
        env_overrides={**policy.env_overrides, **variables}, timeout=None)
    result = await SandboxRunner(policy).run(script, cwd=cwd)
    if result.cleanup_pending:
        raise EnvironmentScriptError(result.cleanup_reason, cleanup_pending=True)
    if result.execution_error:
        raise EnvironmentScriptError(result.execution_error)
    return EnvironmentScriptResult(result.stdout, result.stderr, result.exit_code)


def capture_wrapper(script: str, directory: Path) -> str:
    before, after = directory / "before.env", directory / "after.env"
    if os.name == "nt":
        quote = lambda path: "'" + str(path).replace("'", "''") + "'"
        script_path = directory / "script.ps1"
        script_path.write_text(script, encoding="utf-8-sig")
        def snapshot(path):
            return "$snapshot = @{}; Get-ChildItem Env: | ForEach-Object { $snapshot[$_.Name] = $_.Value }; ConvertTo-Json -InputObject $snapshot -Compress | Set-Content -LiteralPath " + quote(path) + " -Encoding utf8"
        wrapper = directory / "wrapper.ps1"
        wrapper.write_text("\n".join(["$ErrorActionPreference = 'Stop'", "$PSNativeCommandUseErrorActionPreference = $true", snapshot(before),
            "try { & " + quote(script_path) + " } finally {", "$code = if ($null -eq $LASTEXITCODE) { if ($?) { 0 } else { 1 } } else { [int]$LASTEXITCODE }",
            "if ($code -eq 0) { " + snapshot(after) + " }", "}", "exit $code"]), encoding="utf-8-sig")
        return "& " + quote(wrapper)
    script_path = directory / "script.sh"
    script_path.write_text(script, encoding="utf-8")
    on_exit = 'code=$?; if [ "$code" -eq 0 ]; then env -0 > ' + shlex.quote(str(after)) + '; fi'
    return "\n".join(["set -eo pipefail", f"env -0 > {shlex.quote(str(before))}",
        "trap " + shlex.quote(on_exit) + " EXIT",
        ". " + shlex.quote(str(script_path))])


def captured_shell_delta(directory: Path) -> dict:
    def read(path):
        if os.name == "nt": return json.loads(path.read_text(encoding="utf-8-sig"))
        return dict(item.split("=", 1) for item in path.read_text(encoding="utf-8").split("\0") if "=" in item)
    before, after = read(directory / "before.env"), read(directory / "after.env")
    key = (lambda value: value.upper()) if os.name == "nt" else (lambda value: value)
    reserved = {"CODEX_SOURCE_TREE_PATH", "CODEX_WORKTREE_PATH", "CODEX_SETUP_EXIT_CODE", "OLDPWD", "PWD", "SHELLOPTS", "SHLVL", "_"}
    allowed = lambda name, value: key(name) not in reserved and not name.startswith("BASH_FUNC_") and "\n" not in value and "\r" not in value
    previous = {key(name): value for name, value in before.items()}
    current = {key(name) for name in after}
    return {"set": {name: value for name, value in after.items() if allowed(name, value) and previous.get(key(name)) != value},
        "exclude": [name for name, value in before.items() if allowed(name, value) and key(name) not in current]}


def run_worktree_environment(root: Path, worktree: Path, phase: str, *, runner=None) -> str:
    from backend.workspace.trust import is_workspace_trusted

    root, worktree = root.resolve(), worktree.resolve()
    if phase == "setup":
        bind_worktree_environment(root, worktree)
        store_worktree_shell_environment(worktree, {"set": {}, "exclude": []})
    selected = effective_environment(root, worktree)
    if selected["environment"] is None:
        return ""
    if not is_workspace_trusted(root):
        raise PermissionError(f"Local environment project is not trusted: {root}")
    import sys
    lifecycle = LifecycleScript.model_validate(selected["environment"][phase])
    script = lifecycle.for_platform(sys.platform)
    if not script.strip():
        return ""
    project_root = Path(selected["config_path"]).parents[2]
    cwd = project_root if project_root.is_relative_to(worktree) else worktree / project_root.relative_to(source_checkout_root(root))
    variables = {"CODEX_SOURCE_TREE_PATH": str(root), "CODEX_WORKTREE_PATH": str(worktree / root.relative_to(source_checkout_root(root)))} if phase == "setup" else {}
    directory = Path(tempfile.mkdtemp(prefix="minicode-local-environment-"))
    wrapped = capture_wrapper(script, directory)
    try:
        result = runner(wrapped, cwd, variables, capture_directory=directory) if runner is not None else asyncio.run(execute_environment_script(wrapped, cwd, variables, source_root=worktree_source_workspace(root, worktree), capture_directory=directory))
        if result.exit_code == 0 and phase == "setup":
            store_worktree_shell_environment(worktree, captured_shell_delta(directory))
    except BaseException as error:
        if not getattr(error, "cleanup_pending", False): shutil.rmtree(directory)
        raise
    else:
        shutil.rmtree(directory)
    output = result.stdout + result.stderr
    logger.info("Local environment %s (%s), exit %s:\n%s", phase, worktree, result.exit_code, output)
    if result.exit_code != 0:
        raise EnvironmentScriptError(f"Local environment {phase} exited with {result.exit_code}:\n{output}")
    return output


def context_environment_runner(context, loop, *, application_config=None):
    """Bridge a Git worker back to its original accepted executor and policy."""
    def run(script, cwd, variables, *, capture_directory=None):
        return asyncio.run_coroutine_threadsafe(execute_environment_script(script, cwd, variables, context=context,
            capture_directory=capture_directory, application_config=application_config), loop).result()
    return run


async def run_context_worktree_environment(root: Path, worktree: Path, phase: str, context, *, application_config=None) -> str:
    from backend.async_cleanup import to_thread_cancel_safe

    return await to_thread_cancel_safe(run_worktree_environment, root, worktree, phase,
        runner=context_environment_runner(context, asyncio.get_running_loop(), application_config=application_config))
