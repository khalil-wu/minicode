from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import sys
import uuid
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from backend.hooks.policy import event_policy
from backend.hooks.task_output import (
    HookOutputCaptureError,
    HookTaskOutput,
    drain_hook_process_output,
)
from backend.runtime_env import sanitized_subprocess_env
from backend.subprocesses import spawn_exec, spawn_shell, terminate_process_tree

logger = logging.getLogger(__name__)


class HookExecutionError(RuntimeError):
    """A hook runtime/launcher failure distinct from a hook's own exit code."""


_DEFAULT_OUTPUT_TOKEN_LIMIT = 2_500
_CHARS_PER_APPROX_TOKEN = 4


@dataclass
class HookRuntimeBindings:
    workspace_root: Path | None = None
    llm: Any | None = None
    tool_registry: Any | None = None
    tool_context: Any | None = None
    allowed_http_hook_urls: tuple[str, ...] | None = None
    http_hook_allowed_env_vars: tuple[str, ...] | None = None


async def execute_hook(
    entry: Any,
    *,
    event: Any,
    json_input: str,
    event_name: str,
    runtime: HookRuntimeBindings,
    substitute_arguments: Callable[[str, str], str],
    parse_verdict: Callable[[str], tuple[bool, str] | None],
) -> tuple[str, str, int]:
    if entry.hook_type == "command":
        return await _execute_command(
            entry,
            event=event,
            json_input=json_input,
            event_name=event_name,
            runtime=runtime,
        )
    if entry.hook_type == "http":
        return await _execute_http(entry, event=event, json_input=json_input, runtime=runtime)
    if entry.hook_type == "prompt":
        return await _execute_prompt(
            entry,
            event=event,
            json_input=json_input,
            runtime=runtime,
            substitute_arguments=substitute_arguments,
            parse_verdict=parse_verdict,
        )
    if entry.hook_type == "agent":
        return await _execute_agent(
            entry,
            event=event,
            json_input=json_input,
            runtime=runtime,
            substitute_arguments=substitute_arguments,
            parse_verdict=parse_verdict,
        )
    return "", f"Unsupported hook type: {entry.hook_type}", 1


async def _execute_command(
    entry: Any,
    *,
    event: Any,
    json_input: str,
    event_name: str,
    runtime: HookRuntimeBindings,
) -> tuple[str, str, int]:
    command = str(entry.command or "").strip()
    if not command:
        raise HookExecutionError("Hook command is empty")
    if entry.plugin_root:
        plugin_root = Path(entry.plugin_root)
        if not plugin_root.is_dir():
            raise HookExecutionError(f"Plugin directory does not exist: {plugin_root}")

    timeout = float(entry.async_timeout or event_policy(event).default_timeout_seconds)
    shell_type = str(entry.shell or "bash").strip().lower()
    if shell_type not in {"bash", "powershell"}:
        raise HookExecutionError(f"Unsupported hook shell: {shell_type}")

    env = sanitized_subprocess_env()
    env.update({
        "MINICODE_HOOK_EVENT": event_name,
        **dict(entry.env or {}),
    })
    if runtime.workspace_root is not None:
        env["MINICODE_PROJECT_DIR"] = _to_hook_path(runtime.workspace_root, shell_type)
    if entry.plugin_root:
        plugin_root = _to_hook_path(Path(entry.plugin_root).resolve(), shell_type)
        env["MINICODE_PLUGIN_ROOT"] = plugin_root
        command = command.replace("${MINICODE_PLUGIN_ROOT}", plugin_root)
    if entry.plugin_data_root:
        plugin_data = _to_hook_path(Path(entry.plugin_data_root).resolve(), shell_type)
        env["MINICODE_PLUGIN_DATA"] = plugin_data
        command = command.replace("${MINICODE_PLUGIN_DATA}", plugin_data)

    cwd = str(runtime.workspace_root) if runtime.workspace_root else None
    stdin_bytes = (json_input + "\n").encode("utf-8")
    try:
        proc = await _spawn_host_hook(
            command,
            shell_type=shell_type,
            cwd=cwd,
            env=env,
        )
    except Exception as exc:
        raise HookExecutionError(f"Failed to spawn hook: {exc}") from exc

    # A hook whose first stdout line is
    # {"async": true, ...} is backgrounded — we stop waiting and keep draining
    # its output detached so the pipe never fills. stdin is fed concurrently so
    # hooks that read before printing cannot deadlock against the pre-read.
    pre_read_line = b""
    stdin_task: asyncio.Task[None] | None = None
    if proc.stdout is not None and entry.async_timeout is None:
        stdin_task = asyncio.create_task(_feed_hook_stdin(proc, stdin_bytes))
        try:
            # Give the optional async handshake a small grace window; normal
            # hook execution must not block indefinitely waiting for a line.
            first_line = await asyncio.wait_for(proc.stdout.readline(), timeout=0.25)
        except asyncio.TimeoutError:
            first_line = None
    if stdin_task is not None:
        # Always settle the feed before anything else touches the stdin pipe.
        await stdin_task
        if first_line:
            try:
                payload = json.loads(first_line.decode("utf-8", "replace").strip())
            except ValueError:
                payload = None
            if isinstance(payload, dict) and payload.get("async") is True:
                async def _drain_detached(process: Any) -> None:
                    try:
                        while True:
                            chunk = await process.stdout.read(65536)
                            if not chunk:
                                break
                    except Exception:
                        pass
                    try:
                        await process.wait()
                    except Exception:
                        pass

                asyncio.create_task(_drain_detached(proc))
                return first_line.decode("utf-8", "replace"), "", 0
            pre_read_line = first_line

    try:
        capture = HookTaskOutput(
            scope_id=_hook_output_scope(runtime),
            task_id=str(getattr(entry, "entry_id", "") or event_name or "hook"),
        )
        stdout, stderr, cancelled = await _communicate_with_cancel(
            proc,
            b"" if stdin_task is not None else stdin_bytes,
            timeout=timeout,
            cancel_event=getattr(runtime.tool_context, "cancel_event", None),
            capture=capture,
        )
        if pre_read_line:
            stdout = pre_read_line.decode("utf-8", "replace") + stdout
    except asyncio.TimeoutError as exc:
        raise HookExecutionError(f"Hook timed out after {timeout:g}s") from exc
    except HookOutputCaptureError as exc:
        raise HookExecutionError(f"Hook output capture failed: {exc}") from exc
    if cancelled:
        raise HookExecutionError("Hook cancelled")
    stdout = stdout.strip()
    stderr = stderr.strip()
    return stdout, stderr, int(proc.returncode if proc.returncode is not None else 0)


async def _feed_hook_stdin(proc: Any, data: bytes) -> None:
    """Write hook stdin and close the pipe immediately after spawn."""
    stdin = getattr(proc, "stdin", None)
    if stdin is None:
        return
    try:
        if data:
            stdin.write(data)
            await stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        with suppress(Exception):
            stdin.close()


async def _execute_http(
    entry: Any,
    *,
    event: Any,
    json_input: str,
    runtime: HookRuntimeBindings,
) -> tuple[str, str, int]:
    from backend.hooks.http_executor import execute_http_hook

    if not event_policy(event).http_allowed:
        return "", f"HTTP hooks are not supported for {getattr(event, 'value', event)}", 1
    sandbox_policy = getattr(runtime.tool_context, "sandbox_policy", None)
    if sandbox_policy is not None and not bool(sandbox_policy.allow_network):
        return "", "HTTP hook blocked by the active turn sandbox network policy", 126
    response = await execute_http_hook(
        url=entry.url,
        json_input=json_input,
        headers=entry.headers,
        hook_allowed_env_vars=entry.allowed_env_vars,
        policy_allowed_urls=getattr(runtime, "allowed_http_hook_urls", None),
        policy_allowed_env_vars=getattr(runtime, "http_hook_allowed_env_vars", None),
        timeout=float(entry.async_timeout or event_policy(event).default_timeout_seconds),
        sandbox_policy=sandbox_policy,
    )
    if response.aborted:
        return response.body, response.error or "HTTP hook cancelled", 124
    if not response.ok:
        return response.body, response.error, response.status_code or 1
    if response.body.strip():
        try:
            parsed = json.loads(response.body)
        except (TypeError, ValueError):
            return response.body, "HTTP hook must return a JSON object", 1
        if not isinstance(parsed, dict):
            return response.body, "HTTP hook must return a JSON object", 1
    return response.body, "", 0


async def _execute_prompt(
    entry: Any,
    *,
    event: Any,
    json_input: str,
    runtime: HookRuntimeBindings,
    substitute_arguments: Callable[[str, str], str],
    parse_verdict: Callable[[str], tuple[bool, str] | None],
) -> tuple[str, str, int]:
    if runtime.llm is None:
        return "", "Prompt hook runtime is not bound to an LLM", 1
    owned_llm: Any | None = None
    try:
        from backend.llm.base import LLMMessage, SideQueryOptions

        llm = runtime.llm
        if entry.model:
            from backend.config import get_llm_provider
            from backend.services.llm_adapter_factory import build_provider_adapter

            selected_llm = build_provider_adapter(
                get_llm_provider(),
                model_override=entry.model,
            )
            llm = selected_llm
            owned_llm = selected_llm
        prompt = substitute_arguments(entry.prompt, json_input)
        messages: list[Any] = [
            LLMMessage(
                role="system",
                content=(
                    "You are evaluating a lifecycle hook. Return only one JSON object. "
                    'Return {"ok": true} when the condition is met, or '
                    '{"ok": false, "reason": "..."} when it is not. '
                    "Do not return markdown or additional keys."
                ),
            )
        ]
        # Prompt hooks query the model with the hook prompt only
        # (plus optional caller-provided messages) — never the conversation
        # transcript. Copying full history here would exfiltrate the session
        # to whatever model the hook entry names.
        messages.append(LLMMessage(role="user", content=prompt))
        timeout = float(entry.async_timeout or 30.0)
        response = await asyncio.wait_for(
            llm.side_query(
                messages,
                options=SideQueryOptions(
                    operation=f"hook_prompt:{getattr(event, 'value', event)}",
                    query_source="background",
                    max_tokens=512,
                    use_small_fast_model=not bool(entry.model),
                    disable_reasoning=True,
                    enable_prompt_cache=False,
                ),
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return "", f"Prompt hook timed out after {entry.async_timeout or 30.0:g}s", 124
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return "", f"Error executing prompt hook: {exc}", 1
    finally:
        if owned_llm is not None:
            close = getattr(owned_llm, "aclose", None)
            if callable(close):
                try:
                    await close()
                except Exception:
                    logger.debug("Failed to close prompt-hook adapter", exc_info=True)

    verdict = parse_verdict(response)
    if verdict is None:
        return _limit_output(response, entry), "Prompt hook JSON validation failed", 1
    ok, reason = verdict
    if ok:
        return response, "", 0
    message = f"Prompt hook condition was not met: {reason}".rstrip()
    return json.dumps(
        {
            "decision": "block",
            "reason": message,
            "continue": False,
            "stop_reason": reason,
        },
        ensure_ascii=False,
    ), "", 0


async def _execute_agent(
    entry: Any,
    *,
    event: Any,
    json_input: str,
    runtime: HookRuntimeBindings,
    substitute_arguments: Callable[[str, str], str],
    parse_verdict: Callable[[str], tuple[bool, str] | None],
) -> tuple[str, str, int]:
    get_tool = getattr(runtime.tool_registry, "get_tool", None)
    task_tool = get_tool("task") if callable(get_tool) else None
    if task_tool is None or runtime.tool_context is None:
        return "", "Agent hook runtime is not bound to the task agent", 1

    prompt = substitute_arguments(entry.prompt, json_input)
    transcript_path = str(
        getattr(runtime.tool_context, "metadata", {}).get("transcript_path") or ""
    )
    verifier_prompt = (
        f"{prompt}\n\n"
        "Inspect the workspace with the available tools. Do not create subagents or change "
        "the workspace. Use as few steps as possible. "
        + (
            f"The conversation transcript is available at {transcript_path}. "
            if transcript_path
            else ""
        )
        + 'When finished, return only {"ok": true} or '
        '{"ok": false, "reason": "..."}.'
    )
    args: dict[str, Any] = {
        "description": f"Verify {getattr(event, 'value', event)} hook",
        "prompt": verifier_prompt,
        "agent_type": "explore",
        "read_only": True,
    }
    if entry.model:
        args["model"] = entry.model

    metadata = getattr(runtime.tool_context, "metadata", {})
    context_builder = metadata.get("_context_builder") if isinstance(metadata, dict) else None
    parent_state = metadata.get("_agent_state") if isinstance(metadata, dict) else None
    permission_checker = getattr(runtime.tool_context, "permission_checker", None)
    permission_context = getattr(runtime.tool_context, "permission", None)
    if context_builder is None or parent_state is None or permission_checker is None:
        return "", "Agent hook runtime is missing canonical tool execution bindings", 1

    from backend.agent.context import clone_context_builder
    from backend.agent.message import AgentEvent
    from backend.agent.state import AgentState
    from backend.agent.tool_execution import execute_tool_batch
    from backend.llm.base import ToolCallEvent
    from backend.hooks.manager import HookManager, bind_hook_manager, unbind_hook_manager

    empty_manager = HookManager(workspace_root=runtime.workspace_root)
    token = bind_hook_manager(empty_manager)
    hook_tool_call_id = f"hook_task_{uuid.uuid4().hex}"
    hook_context_builder = clone_context_builder(context_builder)
    hook_state = AgentState(
        user_message=verifier_prompt,
        max_iterations=max(1, int(getattr(parent_state, "max_iterations", 20) or 20)),
    )
    hook_state.prompt_context["agent_mode"] = "explore"
    hook_tool_context = replace(
        runtime.tool_context,
        session_id=f"{getattr(runtime.tool_context, 'session_id', '')}:hook",
        task_id=hook_tool_call_id,
        metadata={
            **(dict(metadata) if isinstance(metadata, dict) else {}),
            "_context_builder": hook_context_builder,
            "_agent_state": hook_state,
        },
        approval_handler=None,
    )
    hook_tool_context.metadata["_tool_execution_context"] = hook_tool_context
    tool_result: dict[str, Any] | None = None

    async def _consume_canonical_result() -> None:
        nonlocal tool_result
        batch = execute_tool_batch(
            [ToolCallEvent(id=hook_tool_call_id, name="task", arguments=args)],
            ctx=hook_context_builder,
            state=hook_state,
            tool_registry=runtime.tool_registry,
            permission_checker=permission_checker,
            approval_handler=None,
            skill_manager=None,
            permission_context=permission_context,
            tool_ctx=hook_tool_context,
        )
        try:
            async for event_item in batch:
                if (
                    isinstance(event_item, AgentEvent)
                    and event_item.type == "tool_result"
                    and str(event_item.data.get("id") or "") == hook_tool_call_id
                ):
                    tool_result = dict(event_item.data)
        finally:
            await batch.aclose()

    try:
        await asyncio.wait_for(
            _consume_canonical_result(),
            timeout=float(entry.async_timeout or 60.0),
        )
    except asyncio.TimeoutError:
        return "", f"Agent hook timed out after {entry.async_timeout or 60.0:g}s", 124
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return "", f"Error executing agent hook: {exc}", 1
    finally:
        unbind_hook_manager(token)

    if tool_result is None:
        return "", "Agent hook did not produce a canonical tool result", 1
    content = str(tool_result.get("summary") or "").strip()
    if bool(tool_result.get("is_error")):
        return _limit_output(content, entry), "Agent hook verifier failed", 1
    verdict = parse_verdict(content)
    if verdict is None:
        # Missing structured output cancels the prompt hook without denying.
        return "", "Agent hook did not return structured output", 1
    ok, reason = verdict
    if ok:
        return content, "", 0
    message = f"Agent hook condition was not met: {reason}".rstrip()
    return json.dumps(
        {
            "decision": "block",
            "reason": message,
            "continue": False,
            "stop_reason": reason,
        },
        ensure_ascii=False,
    ), "", 0


async def _spawn_host_hook(
    command: str,
    *,
    shell_type: str,
    cwd: str | None,
    env: dict[str, str],
) -> asyncio.subprocess.Process:
    kwargs = {
        "stdin": asyncio.subprocess.PIPE,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "cwd": cwd,
        "env": env,
    }
    if shell_type == "powershell":
        executable = shutil.which("pwsh") or shutil.which("powershell")
        if not executable:
            raise RuntimeError("PowerShell hook requested but pwsh/powershell was not found")
        return await spawn_exec(
            executable,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
            **kwargs,
        )
    if sys.platform == "win32":
        bash = _find_git_bash()
        if bash is None:
            raise RuntimeError("Bash hook requested but Git Bash was not found")
        # MSYS performs its own argv/path conversion. Passing a native
        # ``C:\\...`` script path through ``bash -lc`` can collapse it to the
        # drive root (``C:\\``) before Python starts. The Windows bash
        # runner uses POSIX paths at this boundary, so normalize every native
        # drive path embedded in the command, not only plugin substitutions.
        command = _windows_paths_to_posix(command)
        if re.search(r"\.sh(?:\s|$|\")", command.strip()) and not command.lstrip().startswith("bash "):
            command = f"bash {command}"
        return await spawn_exec(str(bash), "-lc", command, **kwargs)
    return await spawn_shell(command, **kwargs)


async def _communicate_with_cancel(
    proc: asyncio.subprocess.Process,
    input_data: bytes,
    *,
    timeout: float,
    cancel_event: asyncio.Event | None,
    capture: HookTaskOutput,
) -> tuple[str, str, bool]:
    operation = asyncio.create_task(
        _communicate_hook_process(
            proc,
            input_data,
            timeout=timeout,
            capture=capture,
        )
    )
    cancellation: asyncio.Task[bool] | None = None
    if cancel_event is not None:
        if cancel_event.is_set():
            await _terminate_hook_operation(proc, operation, capture)
            return "", "", True
        cancellation = asyncio.create_task(cancel_event.wait())
    try:
        if cancellation is None:
            stdout, stderr = await operation
            return stdout, stderr, False
        done, _ = await asyncio.wait(
            {operation, cancellation},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if operation in done:
            stdout, stderr = operation.result()
            return stdout, stderr, False
        await _terminate_hook_operation(proc, operation, capture)
        return "", "", True
    except asyncio.CancelledError:
        # Session shutdown can cancel this wrapper before the child coroutine
        # has even entered its own ``try`` block.  Own the process-tree cleanup
        # here as well so an early cancellation cannot orphan the hook.
        await _terminate_hook_operation(proc, operation, capture)
        raise
    finally:
        if cancellation is not None and not cancellation.done():
            cancellation.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await cancellation


async def _communicate_hook_process(
    proc: asyncio.subprocess.Process,
    input_data: bytes,
    *,
    timeout: float,
    capture: HookTaskOutput,
) -> tuple[str, str]:
    """Run one hook process under MiniCode's bounded output and tree-kill rules."""

    operation = asyncio.create_task(
        drain_hook_process_output(proc, input_data, capture=capture)
    )
    try:
        done, _ = await asyncio.wait({operation}, timeout=max(0.0, timeout))
    except asyncio.CancelledError:
        await _terminate_hook_operation(proc, operation, capture)
        raise

    if operation not in done:
        await _terminate_hook_operation(proc, operation, capture)
        raise asyncio.TimeoutError

    try:
        await operation
    except Exception:
        await _terminate_hook_operation(proc, operation, capture)
        raise

    await capture.finish()
    return capture.stdout_text(), capture.stderr_text()


async def _terminate_hook_operation(
    proc: asyncio.subprocess.Process,
    operation: asyncio.Task[Any],
    capture: HookTaskOutput,
) -> None:
    """Terminate one hook tree, drain/cancel its pipes, and close capture once."""

    cancellation_requested = False
    terminate_task = asyncio.create_task(terminate_process_tree(proc))
    try:
        cancellation_requested |= await _wait_cleanup_task(terminate_task)
    except asyncio.CancelledError:
        # The termination task itself was cancelled; continue with pipe and
        # capture cleanup so the caller still owns every spawned resource.
        cancellation_requested = True
    except Exception:
        logger.warning("Failed to terminate hook process tree", exc_info=True)
    if not operation.done():
        drain_waiter = asyncio.create_task(
            _wait_for_hook_pipe_drain(operation),
        )
        try:
            cancellation_requested |= await _wait_cleanup_task(drain_waiter)
        except asyncio.TimeoutError:
            logger.warning("Timed out draining hook pipes during cleanup")
        except asyncio.CancelledError:
            cancellation_requested = True
        except Exception:
            logger.warning("Hook pipe drain failed during cleanup", exc_info=True)
    if not operation.done():
        operation.cancel()
    try:
        cancellation_requested |= await _wait_cleanup_task(operation)
    except asyncio.CancelledError:
        # Expected after the explicit operation.cancel() above.
        pass
    except Exception:
        logger.warning("Hook operation failed while being drained during cleanup", exc_info=True)
    finish_task = asyncio.create_task(capture.finish())
    try:
        cancellation_requested |= await _wait_cleanup_task(finish_task)
    except asyncio.CancelledError:
        cancellation_requested = True
    except Exception:
        logger.warning("Hook output capture failed during cleanup", exc_info=True)
    if cancellation_requested:
        raise asyncio.CancelledError


async def _wait_for_hook_pipe_drain(operation: asyncio.Task[Any]) -> None:
    await asyncio.wait_for(asyncio.shield(operation), timeout=2.0)


async def _wait_cleanup_task(task: asyncio.Task[Any]) -> bool:
    """Drain one owned task and report cancellation of the current wrapper.

    Repeated ``Task.cancel()`` calls must not interrupt process-tree or file
    cleanup.  The inner task is shielded until it reaches a terminal state;
    the caller re-raises cancellation only after all owned resources converge.
    """

    cancellation_requested = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancellation_requested = True
    task.result()
    return cancellation_requested


def _hook_output_scope(runtime: HookRuntimeBindings) -> str:
    tool_context = runtime.tool_context
    metadata = getattr(tool_context, "metadata", {}) if tool_context is not None else {}
    if not isinstance(metadata, dict):
        metadata = {}
    return str(
        metadata.get("hook_session_id")
        or metadata.get("conversation_id")
        or getattr(tool_context, "conversation_id", "")
        or getattr(tool_context, "session_id", "")
        or "startup"
    ).strip()


def _find_git_bash() -> Path | None:
    git = shutil.which("git.exe") or shutil.which("git")
    candidates: list[Path] = []

    def add_root(root: Path) -> None:
        candidates.extend((root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"))

    if git:
        git_path = Path(git).resolve()
        # Git for Windows can expose git.exe from <root>/cmd, <root>/bin, or
        # <root>/mingw64/bin.  Walking parents and checking the canonical bash
        # locations finds portable/custom installs instead of assuming a fixed
        # Program Files layout.
        for parent in git_path.parents:
            add_root(parent)

    for program_files in filter(None, (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"))):
        add_root(Path(program_files) / "Git")

    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.is_file():
            return candidate
    return None


def _to_hook_path(path: str | Path, shell_type: str) -> str:
    value = str(path)
    if sys.platform != "win32" or shell_type == "powershell":
        return value
    normalized = value.replace("\\", "/")
    drive = re.match(r"^([A-Za-z]):/(.*)$", normalized)
    if drive:
        return f"/{drive.group(1).lower()}/{drive.group(2)}"
    return normalized


def _windows_paths_to_posix(command: str) -> str:
    pattern = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z]):[\\/]+([^\s\"'`]+)")

    def replace(match: re.Match[str]) -> str:
        suffix = match.group(2).replace("\\", "/")
        return f"/{match.group(1).lower()}/{suffix}"

    return pattern.sub(replace, command)


def _limit_output(value: str, entry: Any) -> str:
    text = str(value or "").strip()
    token_limit = entry.additional_context_limit
    if token_limit is None:
        token_limit = _DEFAULT_OUTPUT_TOKEN_LIMIT
    if token_limit == 0:
        return text
    char_limit = max(1, int(token_limit)) * _CHARS_PER_APPROX_TOKEN
    if len(text) <= char_limit:
        return text
    omitted = len(text) - char_limit
    return f"{text[:char_limit]}\n\n[Hook output truncated: {omitted} characters omitted.]"
