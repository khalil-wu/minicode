"""Run selected environment hooks through their captured executor attachments."""
from __future__ import annotations

from dataclasses import replace
import json

from backend.hooks.manager import HookManager
from backend.hooks.runners import HookExecutionError, HookExecutionResult, _entry_timeout_seconds
from backend.permissions.environment_command import EnvironmentCommandPolicy, executor_shell_argv
from backend.sandbox.policy import PermissionProfile
from backend.terminal.executor_runner import ExecutorCommandRunner
from backend.workspace.executor_paths import ExecutorPath


def with_tool_environment_hooks(context, environments):
    parent = context.run_context.hook_manager if context.run_context is not None else None
    sources = [environment for environment in environments if environment.config.environment_hooks]
    if not sources:
        return parent
    manager = parent.fork_for_turn(workspace_root=parent.workspace_root) if parent is not None else HookManager()
    manager.hooks = {event: list(entries) for event, entries in manager.hooks.items()}
    for environment in sources:
        admitted = HookManager.from_settings({"hooks": environment.config.environment_hooks})
        for event, entries in admitted.hooks.items():
            manager.hooks.setdefault(event, []).extend(replace(entry,
                entry_id=f"environment:{environment.selection.environment_id}:{entry.entry_id}",
                execution_environment=environment) for entry in entries)
    manager.bind_runtime(llm=context.llm, tool_registry=context.tool_registry, tool_context=context,
        mcp_executor=parent._mcp_executor if parent is not None else None)
    return manager


async def execute_environment_hook(entry, *, event, json_input, event_name, runtime):
    environment = entry.execution_environment
    shell = environment.shell
    if shell is None:
        raise HookExecutionError("hook environment did not report a shell")
    command = entry.command
    if shell["name"] == "cmd":
        raise HookExecutionError("environment command hooks require PowerShell on Windows")
    if shell["name"] == "powershell":
        script = ("$global:LASTEXITCODE = 0\n"
            f"[Console]::In.ReadLine() | {command}\n"
            "if ($?) { exit 0 }\nif ($LASTEXITCODE) { exit $LASTEXITCODE }\nexit 1")
    else:
        script = f"IFS= read -r codex_hook_input; printf '%s' \"$codex_hook_input\" | (\n{command}\n)"
    argv = executor_shell_argv(environment, script)
    # Source environment hooks keep the owner's filesystem access while the
    # existing executor runner still enforces managed network policy.
    scope = replace(environment.permission_scope, permission_profile=PermissionProfile.disabled())
    cwd = ExecutorPath.parse(scope.cwd_uri, platform_os=scope.platform_os)
    input_data = {**json.loads(json_input), "cwd": cwd.native}
    payload = (json.dumps(input_data, ensure_ascii=True) + "\n").encode("utf-8")
    context = runtime.tool_context
    network_step = context.run_context.network_step if context.run_context is not None else None
    network = network_step.policy_for(environment) if network_step is not None else None
    policy = EnvironmentCommandPolicy(scope, environment, environment.config.shell_environment_policy,
        {"MINICODE_HOOK_EVENT": event_name, "MINICODE_PROJECT_DIR": cwd.native, **entry.env},
        _entry_timeout_seconds(entry, event), True, network_policy=network)
    runner = ExecutorCommandRunner(policy)
    output = {"stdout": bytearray(), "stderr": bytearray()}

    async def collect(text, stream):
        target = output[stream]
        target.extend(text.encode("utf-8")[:max(0, 1024 * 1024 - len(target))])

    async def send_input(process):
        if await process.handle.write(payload) != "accepted":
            raise HookExecutionError("hook process did not accept its input")

    result = await runner.run(command, cwd=scope.cwd_uri, argv=argv, keep_stdin_open=True,
        cancel_event=context.cancel_event, stream_callback=collect, process_ready_callback=send_input)
    if result.timed_out:
        raise HookExecutionError("environment command hook timed out")
    if result.cancelled:
        raise HookExecutionError("environment command hook cancelled")
    if result.execution_error:
        raise HookExecutionError(result.execution_error)
    stdout, stderr = (output[name].decode("utf-8", errors="replace") for name in ("stdout", "stderr"))
    return HookExecutionResult(stdout, stderr, result.exit_code, semantic_stdout=stdout)
