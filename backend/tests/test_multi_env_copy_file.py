"""Binary copies through real executor transports and the tool admission chain."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import replace
import errno
import json
import os
import shutil
import sys

import pytest
import pytest_asyncio

from backend.agent.context import ContextBuilder
from backend.agent.environment_selection import EnvironmentConfig, EnvironmentConfigState, ThreadEnvironmentDefaults, ThreadEnvironments, TurnEnvironmentRequest, TurnEnvironmentSelection
from backend.agent.final_tool_request import FinalExecutableToolRequest
from backend.agent.state import AgentState
from backend.agent.tool_environments import ToolEnvironmentBindings, execution_environment_identity
from backend.config import PermissionSettings
from backend.environments import EnvironmentManager
from backend.environments.filesystem import FILE_CHUNK_SIZE
from backend.feature_flags import FeatureFlags
from backend.llm.base import ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.grants import PermissionGrantResponse, normalize_permission_profile
from backend.sandbox.policy import FileSystemSandboxPolicy, PermissionProfile
from backend.services.tool_registry_factory import build_tool_registry
from backend.tests.test_environments import PEER, configure
from backend.tests.test_executor_tool_admission import batch
from backend.tests.test_native_exec_policy_chain import setup
from backend.tools.base import validate_tool_input
from backend.tools.multi_env_copy_file import MultiEnvCopyFileTool
from backend.tools.toolsets import ToolsetPolicy
from backend.workspace.executor_local import LocalExecutionEnvironmentBackend
from backend.workspace.executor_paths import ExecutorPath


@pytest_asyncio.fixture
async def copy_host(tmp_path):
    host = setup(tmp_path, approval_policy="never")
    async with AsyncExitStack() as cleanup:
        cleanup.callback(host.context.artifact_store.shutdown)
        folder = tmp_path / "executors"
        folder.mkdir()
        shell = ({"MINICODE_TEST_SHELL_NAME": "powershell", "MINICODE_TEST_SHELL_PATH": shutil.which("powershell")}
            if os.name == "nt" else {"MINICODE_TEST_SHELL_NAME": "bash", "MINICODE_TEST_SHELL_PATH": shutil.which("bash")})
        configure(folder, [{"id": name, "program": sys.executable, "args": [str(PEER)],
            "cwd": str(host.workspace), "env": shell} for name in ("source", "target")], default="source", include_local=True)
        host.manager = EnvironmentManager(folder, local_backend=LocalExecutionEnvironmentBackend())
        cleanup.push_async_callback(host.manager.close)
        for name in ("source", "target", "local"):
            (host.workspace / name).mkdir()
        defaults = ThreadEnvironmentDefaults(EnvironmentConfig(PermissionProfile.disabled(), (host.workspace.as_uri(),)), "disabled")
        async def resolve(_selection, config, _environment, _peer):
            return config
        host.env_owner = ThreadEnvironments(host.manager, defaults, resolve_config=resolve)
        cleanup.push_async_callback(host.env_owner.aclose)
        async def select(*, profiles=None, hooks=None):
            selections = []
            for name in ("target", "local", "source"):
                cwd = (host.workspace / name).as_uri()
                config = EnvironmentConfig((profiles or {}).get(name, PermissionProfile.disabled()), (cwd,),
                    allow_login_shell=False, environment_hooks=(hooks or {}).get(name, {}))
                selections.append(TurnEnvironmentSelection.from_request(TurnEnvironmentRequest(
                    name, cwd, (cwd,), EnvironmentConfigState.ready(config))))
            await host.env_owner.update_selections(selections)
            host.environments = await host.env_owner.snapshot()
            cleanup.push_async_callback(host.environments.aclose)
            host.context.run_context.turn_environments = host.environments
        host.select = select
        await select()
        host.context.run_context.thread_environments = host.env_owner
        host.context = replace(host.context, permission=replace(host.context.permission, mode="bypass", sandbox_mode="danger-full-access"),
            sandbox_policy=None, permission_checker=PermissionChecker(PermissionSettings(require_confirm=[], require_diff_review=[]), host.workspace))
        host.checker = host.context.permission_checker
        host.tool = MultiEnvCopyFileTool()
        host.registry.register(host.tool)
        host.state = AgentState(user_message="Copy an actual binary artifact", conversation_id="source-owner", workspace_root=host.workspace)
        host.builder = ContextBuilder(conversation_id="source-owner", workspace_root=host.workspace)
        yield host


def arguments(host, source="source", target="target", *, source_name="source.bin", target_name="target.bin"):
    return {"source_environment_id": source, "source_path": str(host.workspace / source / source_name),
        "target_environment_id": target, "target_path": str(host.workspace / target / target_name)}


def attachments(host):
    return {item.selection.environment_id: item for item in host.environments.turn_environments()}


@pytest.mark.asyncio
@pytest.mark.parametrize("source,target", [("source", "target"), ("local", "target"), ("source", "local")])
async def test_actual_binary_copy_and_frozen_endpoint_permissions(copy_host, monkeypatch, source, target):
    host = copy_host
    contents = b"binary must stay outside model context\x00\xff" * 70000
    (host.workspace / source / "source.bin").write_bytes(contents)
    captured = []
    execute = host.tool.execute
    async def capture(args, context=None):
        captured.append(context.final_tool_request)
        return await execute(args, context)
    monkeypatch.setattr(host.tool, "execute", capture)
    _, result, events = await batch(host, host.tool.name, arguments(host, source, target))
    assert not result["is_error"], result
    assert json.loads(result["summary"]) == {"bytes_copied": len(contents)}
    assert (host.workspace / target / "target.bin").read_bytes() == contents
    assert "binary must stay outside model context" not in json.dumps([event.data for event in events])
    assert captured[0].digest == result["request_digest"]
    assert {key: value["environment_id"] for key, value in captured[0].environment_identity.items()} == {
        "source_environment_id": source, "target_environment_id": target}
    for item in host.environments.turn_environments():
        if item.is_remote:
            assert not item.environment.client.files


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["source_missing", "target_parent_missing", "same", "unknown", "relative", "foreign"])
async def test_rejections_never_overwrite_the_existing_target(copy_host, failure):
    host = copy_host
    source = host.workspace / "source/source.bin"
    target = host.workspace / "target/target.bin"
    source.write_bytes(b"copy")
    target.write_bytes(b"untouched")
    args = arguments(host)
    if failure == "source_missing":
        args["source_path"] += ".missing"
    elif failure == "target_parent_missing":
        args["target_path"] = str(target.parent / "missing/target.bin")
    elif failure == "same":
        args["source_environment_id"] = "target"
    elif failure == "unknown":
        args["source_environment_id"] = "unknown"
    elif failure == "relative":
        args["source_path"] = "source.bin"
    else:
        args["source_path"] = "/foreign/source" if os.name == "nt" else "C:\\foreign\\source"
    _, result, _ = await batch(host, host.tool.name, args)
    assert result["is_error"], result
    assert target.read_bytes() == b"untouched"
    assert not (target.parent / "missing").exists()
    for item in attachments(host).values():
        if item.is_remote:
            assert not item.environment.client.files


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["source", "target"])
async def test_grants_belong_to_the_named_endpoint(copy_host, endpoint):
    host = copy_host
    await host.select(profiles={endpoint: PermissionProfile.managed(FileSystemSandboxPolicy.restricted(()))})
    (host.workspace / "source/source.bin").write_bytes(b"granted copy")
    _, denied, _ = await batch(host, host.tool.name, arguments(host))
    assert denied["is_error"]
    cwd = ExecutorPath.parse((host.workspace / endpoint).as_uri())
    wanted = normalize_permission_profile({"file_system": {"read" if endpoint == "source" else "write": [cwd.native]}}, cwd=cwd)
    grant = PermissionGrantResponse(wanted, "turn", False)
    other = "target" if endpoint == "source" else "source"
    host.context.permission_grants.record(grant, other, host.context.turn_id)
    _, wrong, _ = await batch(host, host.tool.name, arguments(host))
    assert wrong["is_error"]
    host.context.permission_grants.record(grant, endpoint, host.context.turn_id)
    _, result, _ = await batch(host, host.tool.name, arguments(host))
    assert not result["is_error"], result
    assert (host.workspace / "target/target.bin").read_bytes() == b"granted copy"


@pytest.mark.asyncio
async def test_old_executor_uses_single_write_after_complete_read(copy_host, monkeypatch):
    host = copy_host
    contents = b"\x00\xff" * (FILE_CHUNK_SIZE + 1)
    (host.workspace / "source/source.bin").write_bytes(contents)
    target = attachments(host)["target"].environment
    original = target.info
    async def older():
        info = await original()
        return {**info, "capabilities": {**info["capabilities"], "fileWriteStreaming": False}}
    monkeypatch.setattr(target, "info", older)
    _, result, _ = await batch(host, host.tool.name, arguments(host))
    assert not result["is_error"], result
    assert (host.workspace / "target/target.bin").read_bytes() == contents


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_partial_write_and_cancel_release_both_real_handles(copy_host, monkeypatch, cancel):
    host = copy_host
    (host.workspace / "source/source.bin").write_bytes(b"x" * (FILE_CHUNK_SIZE * 3))
    selected = attachments(host)
    client = selected["target"].environment.client
    request = client.request
    reached = asyncio.Event()
    async def delayed(method, params, **kwargs):
        if method == "fs/writeBlock" and params["offset"] == FILE_CHUNK_SIZE:
            reached.set()
            if cancel:
                await asyncio.Event().wait()
            raise OSError(errno.ENOSPC, "signed websocket secret must not reach the model")
        return await request(method, params, **kwargs)
    monkeypatch.setattr(client, "request", delayed)
    call = ToolCallEvent(id="partial", name=host.tool.name, arguments=arguments(host))
    bindings = ToolEnvironmentBindings(host.environments)
    context = bindings.for_call(call, host.context, host.tool)
    task = asyncio.create_task(host.tool.execute(call.arguments, context))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert result.is_error and "partially written" in result.content and "ENOSPC" in result.content
            assert "secret" not in result.content
        assert (host.workspace / "target/target.bin").stat().st_size == FILE_CHUNK_SIZE
        assert not selected["source"].environment.client.files and not client.files
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await bindings.close_after_tools(context)


@pytest.mark.asyncio
async def test_pending_copy_keeps_original_registrations(copy_host, monkeypatch):
    host = copy_host
    (host.workspace / "source/source.bin").write_bytes(b"original registrations")
    originals = attachments(host)
    execute = host.tool.execute
    replacements = []
    async def replace_before_execution(args, context=None):
        for name in ("source", "target"):
            replacements.append(host.manager.upsert({"id": name, "program": "unused-replacement-executor"}))
            assert context.named_turn_environments[f"{name}_environment_id"].environment is originals[name].environment
        recaptured = FinalExecutableToolRequest.freeze(ToolCallEvent(id="recapture", name=host.tool.name, arguments=args),
            environment_identity=execution_environment_identity(context))
        assert recaptured.digest == context.final_tool_request.digest
        return await execute(args, context)
    monkeypatch.setattr(host.tool, "execute", replace_before_execution)
    _, result, _ = await batch(host, host.tool.name, arguments(host))
    assert not result["is_error"], result
    assert (host.workspace / "target/target.bin").read_bytes() == b"original registrations"
    assert all(item.client.connection is None for item in replacements)


@pytest.mark.asyncio
@pytest.mark.parametrize("swap", [False, True])
async def test_both_environment_hooks_run_and_cannot_swap_endpoints(copy_host, swap):
    host = copy_host
    source = host.workspace / "source/source.bin"
    source.write_bytes(b"hooked")
    rewrite = {"source_path": str(source), "target_path": str(host.workspace / "target/rewritten.bin")}
    if swap:
        rewrite.update(source_environment_id="target", target_environment_id="source")
    output = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow", "updatedInput": rewrite}}
    def hook(marker, payload):
        text = json.dumps(payload)
        if os.name == "nt":
            command = f"Out-Null; Add-Content -Path {marker} -Value run; Write-Output '{text.replace(chr(39), chr(39) * 2)}'"
        else:
            import shlex
            command = f"cat >/dev/null; echo run >> {marker}; printf '%s' {shlex.quote(text)}"
        return [{"matcher": "multi_env_copy_file", "hooks": [{"type": "command", "command": command}]}]
    await host.select(hooks={name: {"PreToolUse": hook("pre", output if name == "source" else {}),
        "PostToolUse": hook("post", {})} for name in ("source", "target", "local")})
    _, result, _ = await batch(host, host.tool.name, arguments(host))
    if swap:
        assert result["is_error"] and "cannot change the tool environment" in result["summary"], result
        assert not (host.workspace / "target/rewritten.bin").exists()
    else:
        assert not result["is_error"], result
        assert (host.workspace / "target/rewritten.bin").read_bytes() == b"hooked"
    assert not (host.workspace / "target/target.bin").exists()
    for name in ("source", "target", "local"):
        for marker in ("pre", "post"):
            path = host.workspace / name / marker
            if name != "local" and (marker == "pre" or not swap):
                assert path.read_text().splitlines() == ["run"]
            else:
                assert not path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_feature_gate_and_stable_schema_without_selected_environments(copy_host, enabled):
    host = copy_host
    registry = build_tool_registry(host.context.artifact_store, workspace_root=host.workspace,
        config=replace(host.snapshot.config, feature_flags=FeatureFlags({"multi_env_copy_file": enabled})), mcp_manager=None)
    for count in (0, 1, 2):
        policy = ToolsetPolicy(environment_count=count, advertise_environment_tools=count > 0)
        schemas = registry.get_schemas(toolset_policy=policy)
        tool = next((item["function"] for item in schemas if item["function"]["name"] == "multi_env_copy_file"), None)
        assert (tool is not None) == enabled
        if enabled:
            assert set(tool["parameters"]["properties"]) == set(arguments(host))
    args = arguments(host)
    assert validate_tool_input(host.tool, {**args, "environment_id": "source"})
    assert "IDs must differ" in validate_tool_input(host.tool, {**args, "target_environment_id": "source"})
