"""Explicit workbench commands on the request's original executor and policy."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import shlex
import subprocess
from uuid import uuid4

from fastapi import HTTPException

from backend.permissions.environment_command import EnvironmentCommandPolicy, executor_shell_argv
from backend.terminal.executor_runner import ExecutorCommandRunner
from backend.workspace.executor_service import executor_registration_id
from backend.environments.errors import ExecutorClosed, ExecutorDisconnected, ExecutorProtocolError


def require_workspace_registration(service, expected):
    if expected != executor_registration_id(service.context):
        raise HTTPException(409, "The selected execution environment changed. Refresh the workspace before running this action.")


async def workbench_policy(service, *, timeout, env=None, shell_policy=None, permission_scope=None):
    from backend.network.policy import capture_thread_network_policy
    from backend.network.runtime import prepare_environment_network_step
    from backend.permissions.exec_policy import ExecPolicyOwner
    from backend.async_cleanup import to_thread_cancel_safe

    run = service.context.run_context
    stack = service.application_config.config_layer_stack
    draft = capture_thread_network_policy(stack)
    if run.network_step is None and (draft.controller is not None or draft.requirements is not None
                                     or service.config.network_policy is not None):
        home = Path(next(layer.source.file for layer in reversed(stack.get_layers()) if layer.source.kind == "user")).parent
        owner = await to_thread_cancel_safe(ExecPolicyOwner, home=home, stack=stack)
        run.network_step = await prepare_environment_network_step(environment_manager=run.environment_manager,
            snapshot=run.turn_environments, config_stack=stack, exec_policy_sources=owner.sources)
    attachment = service.context.turn_environment
    scope = replace(permission_scope or service.scope, cwd_uri=service.cwd.uri)
    network = run.network_step.policy_for(attachment) if run.network_step is not None else None
    return EnvironmentCommandPolicy(scope, attachment, shell_policy or service.config.shell_environment_policy, env or {}, timeout,
        service.context.permission.allow_unsandboxed_commands,
        network_policy=network.for_scope(scope) if network is not None else None)


async def run_workbench_command(service, argv, *, timeout=120, env=None, stream_callback=None, allow_failure=False,
                                shell_policy=None, permission_scope=None, input_text=None, programs=None, on_runner=None):
    policy = await workbench_policy(service, timeout=timeout, env=env,
        shell_policy=shell_policy, permission_scope=permission_scope)
    runner = ExecutorCommandRunner(policy)
    argv = list(argv)
    command = subprocess.list2cmdline(argv) if service.cwd.convention == "windows" else shlex.join(argv)
    try:
        if on_runner is not None:
            on_runner(runner)
        if input_text is not None:
            temporary_root = service.resolve(programs["__temp__"])
            message_file = temporary_root.joinpath(f"minicode-git-message-{uuid4().hex}.txt")
            write_finished = asyncio.Event()
            async def write_message_file():
                try:
                    await service.filesystem.write_file(message_file.uri, input_text.encode("utf-8"), sandbox=service.sandbox)
                finally:
                    write_finished.set()
            write_task = asyncio.create_task(write_message_file(), name="executor-git-message-write")
            async def remove_message_file():
                await write_finished.wait()
                if not write_task.cancelled() and isinstance(write_task.exception(), (ExecutorClosed, ExecutorDisconnected)):
                    raise ExecutorProtocolError(f"Target message-file write is unconfirmed: {message_file.uri}") from write_task.exception()
                if await service.metadata(message_file) is not None:
                    await service.remove(message_file)
            await runner.add_cleanup_callback(remove_message_file)
            await asyncio.shield(write_task)
            argv.append(message_file.native)
        command = subprocess.list2cmdline(argv) if service.cwd.convention == "windows" else shlex.join(argv)
        result = await runner.run(command, cwd=service.cwd.uri, argv=argv, stream_callback=stream_callback)
    finally:
        if not runner.closed:
            try:
                await runner.cleanup()
            finally:
                if not runner.closed:
                    await service.context.background_manager.retain_foreground_cleanup(runner=runner, command=command,
                        cwd=service.cwd.uri, conversation_id=service.context.conversation_id, task_id="", parent_run_id="",
                        reason="workspace_command_cleanup_pending")
    if result.cancelled:
        raise asyncio.CancelledError
    if result.cleanup_pending:
        raise HTTPException(409, {"message": "The original executor command has not confirmed cleanup.",
            "environment_id": service.scope.environment_id, "process_id": runner.handle.process_id if runner.handle is not None else None})
    if result.timed_out:
        raise HTTPException(504, "The selected executor command timed out.")
    if result.execution_error:
        raise HTTPException(403, result.execution_error)
    if result.exit_code and not allow_failure:
        raise HTTPException(409, result.stderr.strip() or result.stdout.strip() or f"Command exited with {result.exit_code}")
    return result


async def workbench_http_request(http_request, operation):
    """A closed workbench request cancels its original target operation."""
    async def disconnected():
        while True:
            if (await http_request.receive())["type"] == "http.disconnect":
                return

    command = asyncio.create_task(operation)
    disconnect = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait({command, disconnect}, return_when=asyncio.FIRST_COMPLETED)
        if command in done:
            return await command
        command.cancel()
        try:
            await command
        except asyncio.CancelledError:
            pass
        raise HTTPException(499, "Workspace action cancelled because the client disconnected.")
    finally:
        disconnect.cancel()
        await asyncio.gather(disconnect, return_exceptions=True)
        if not command.done():
            if not command.cancelling():
                command.cancel()
            try:
                await command
            except asyncio.CancelledError:
                pass


async def target_programs(service):
    """Resolve tools and temporary storage in the target's captured shell env."""
    shell = service.context.turn_environment.shell
    if shell is None:
        raise HTTPException(503, "The selected executor does not report a shell for target tool discovery.")
    if shell["name"] == "powershell":
        script = "$ErrorActionPreference='Stop'; $tools=@{}; foreach($n in @('python3','python','py','node','npm','pnpm','yarn','gh','git','cmd')) { " \
            "$c=Get-Command -Name $n -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1; " \
            "if($null -ne $c){$tools[$n]=$c.Source} }; " \
            "$tools['__temp__']=$env:TEMP; $tools['__github_host__']=$env:GH_HOST; $tools | ConvertTo-Json -Compress"
    elif shell["name"] in {"sh", "bash", "zsh"}:
        script = 'for n in python3 python node npm pnpm yarn gh git; do p=$(command -v "$n") || continue; ' \
            'printf "%s\\000%s\\000" "$n" "$p"; done; printf "__temp__\\000%s\\000__github_host__\\000%s\\000" "${TMPDIR:-/tmp}" "${GH_HOST-}"'
    else:
        raise HTTPException(503, f"Target tool discovery requires a POSIX or PowerShell shell; received {shell['name']}")
    argv = executor_shell_argv(service.context.turn_environment, script, login=False)
    result = await service.run(argv)
    if result.returncode:
        raise HTTPException(503, result.stderr.decode("utf-8", errors="replace").strip())
    if shell["name"] == "powershell":
        return json.loads(result.stdout)
    parts = result.stdout.decode("utf-8").split("\0")
    return dict(zip(parts[0:-1:2], parts[1:-1:2]))
