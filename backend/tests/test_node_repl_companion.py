from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from backend.agent.environment_selection import EnvironmentConfig, EnvironmentConfigState, ThreadEnvironmentDefaults, ThreadEnvironments, TurnEnvironmentRequest, TurnEnvironmentSelection
from backend.environments import EnvironmentManager
from backend.mcp.environment_runtime import MCPEnvironmentRuntime
from backend.mcp.manager import MCPServerManager
from backend.mcp.node_repl import NodeReplCompanions
from backend.workspace.executor_local import LocalExecutionEnvironmentBackend
from backend.sandbox.policy import PermissionProfile


@asynccontextmanager
async def companion(tmp_path, *, environment_config=None):
    backend = LocalExecutionEnvironmentBackend()
    environments = EnvironmentManager(tmp_path, local_backend=backend)
    config = environment_config or EnvironmentConfig(PermissionProfile.disabled(), (tmp_path.as_uri(),))
    async def resolve(selection, config, environment, info):
        return config
    thread = ThreadEnvironments(environments, ThreadEnvironmentDefaults(config, "disabled"), resolve_config=resolve)
    await thread.update_selections([TurnEnvironmentSelection.from_request(TurnEnvironmentRequest("local", tmp_path.as_uri(), (tmp_path.as_uri(),), EnvironmentConfigState.ready(config)))])
    snapshot = await thread.snapshot()
    owner = NodeReplCompanions()
    runtime = MCPEnvironmentRuntime(environments, snapshot, local_cwd=tmp_path.as_uri())
    runtime.node_repl_companions = owner
    registered = await owner.admit(snapshot.primary(), None)
    assert registered is not None, "This real-process suite requires the installed Node runtime"
    manager = MCPServerManager(config_path=tmp_path / "mcp.json", workspace_root=tmp_path,
        environment_runtime=runtime, runtime_configs=(registered,))
    try:
        await manager.start_all()
        client = manager.get_client("node_repl")
        assert client is not None, manager.get_all_status()
        yield SimpleNamespace(owner=owner, manager=manager, client=client, runtime=runtime,
            environments=environments, backend=backend, thread=thread, snapshot=snapshot, config=registered)
    finally:
        await manager.stop_all()
        await owner.aclose()
        await runtime.aclose()
        await snapshot.aclose()
        await thread.aclose()
        assert not backend.process.processes, "The original Node process handle must be closed"
        await environments.close()


def body(result):
    return "\n".join(block["text"] for block in result.content if block["type"] == "text")

@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["system", "desktop-bundled"])
async def test_real_node_mcp_catalog_persistent_redeclaration_imports_and_metadata(tmp_path, monkeypatch, runtime, request):
    if runtime == "desktop-bundled":
        binary = Path(__file__).resolve().parents[2] / "desktop/node_modules/electron/dist/electron.exe"
        if not binary.is_file():
            pytest.skip("desktop-bundled Node binary is unavailable on this host")
        monkeypatch.setenv("MINICODE_EDITOR_NODE", str(binary))
    package = tmp_path / "node_modules/ordinary-node-package"
    package.mkdir(parents=True)
    (package / "package.json").write_text('{"type":"module","main":"index.js"}')
    (package / "index.js").write_text('export const version = process.version; export const platform = process.platform;')
    async with companion(tmp_path) as fixture:
        tools = await fixture.client.list_tools()
        assert {tool.name for tool in tools} == {"js", "js_reset", "js_add_node_module_dir"}
        assert "persistent" in fixture.client.instructions
        first = await fixture.client.call_tool("js", {"code": "const answer = 40; nodeRepl.write(answer)"})
        assert not first.is_error and body(first) == "40", body(first)
        second = await fixture.client.call_tool("js", {"code": "const answer = 42; const fs = await import('node:fs/promises'); await fs.writeFile('owned-node.txt', String(answer)); nodeRepl.write(JSON.stringify({answer,cwd:nodeRepl.cwd,request:nodeRepl.requestMeta,process:typeof process,tools:typeof tools}))"}, request_meta={"owned": "actual-request"})
        assert not second.is_error, body(second)
        assert json.loads(body(second)) == {"answer": 42, "cwd": str(tmp_path), "request": {"owned": "actual-request"}, "process": "undefined", "tools": "undefined"}
        assert (tmp_path / "owned-node.txt").read_text() == "42"
        for code in ("import fs from 'node:fs'", "await import('node:process')"):
            rejected = await fixture.client.call_tool("js", {"code": code})
            assert rejected.is_error
        assert body(await fixture.client.call_tool("js", {"code": "nodeRepl.write(answer)"})) == "42"
        ordinary = await fixture.client.call_tool("js", {"code": "const pkg=await import('ordinary-node-package');nodeRepl.write(pkg.version)"})
        assert not ordinary.is_error and body(ordinary).startswith("v"), body(ordinary)
        frame = await fixture.client.call_tool("js", {"code": "const mod=await import('node:module');nodeRepl.write(mod.createRequire(nodeRepl.cwd+'/frame.cjs')('internal/async_context_frame').enabled)"})
        assert not frame.is_error and body(frame) == "true", body(frame)
        request.node.user_properties.append(("actual_node_context_backend", json.dumps({"executable":fixture.config.command,
            "version":body(ordinary),"async_context_frame_enabled":True})))
        installed_modules = Path(__file__).resolve().parents[2] / "desktop/language-services/node_modules"
        added = await fixture.client.call_tool("js_add_node_module_dir", {"path": str(installed_modules)})
        assert not added.is_error
        jsonc = await fixture.client.call_tool("js", {"code": "const jsonc=await import('jsonc-parser');nodeRepl.write(JSON.stringify(jsonc.default.parse('{\"actual\":\"npm-package\"}//comment')))"})
        assert not jsonc.is_error and json.loads(body(jsonc)) == {"actual": "npm-package"}, body(jsonc)

@pytest.mark.asyncio
async def test_actual_restricted_process_cannot_read_the_denied_file(tmp_path):
    from backend.permissions.environment_scope import EnvironmentPermissionScope
    from backend.sandbox.policy import FileSystemSandboxPolicy, FileSystemSandboxEntry, FileSystemPath, FileSystemSpecialPath, FileSystemAccessMode, NetworkSandboxPolicy
    from backend.sandbox.runner import SandboxRunner
    from backend.workspace.executor_local_policy import local_sandbox_policy

    secret = tmp_path / "denied-node.txt"
    secret.write_text("OWNED_DENIED_VALUE")
    profile = PermissionProfile.managed(FileSystemSandboxPolicy.restricted([
        FileSystemSandboxEntry(FileSystemPath.special(FileSystemSpecialPath.ROOT), FileSystemAccessMode.READ),
        FileSystemSandboxEntry(FileSystemPath("path", secret.as_uri()), FileSystemAccessMode.DENY),
    ]), network=NetworkSandboxPolicy.ENABLED)
    scope = EnvironmentPermissionScope("local", profile, tmp_path.as_uri(), (tmp_path.as_uri(),),
        platform_os="windows", windows_sandbox_level="restricted-token")
    capability = SandboxRunner(local_sandbox_policy(scope.sandbox_context())).capability(cwd=tmp_path)
    if not capability.available:
        pytest.skip("real restricted process prerequisite: " + capability.reason)
    config = EnvironmentConfig(profile, (tmp_path.as_uri(),), windows_sandbox_type="windowsRestrictedToken", windows_sandbox_level="restricted-token")
    async with companion(tmp_path, environment_config=config) as fixture:
        [process] = fixture.backend.process.processes.values()
        assert process.sandbox is not None and process.sandbox["permissions"]["type"] == "managed"
        result = await fixture.client.call_tool("js", {"code": "const fs = await import('fs/promises'); nodeRepl.write(await fs.readFile('denied-node.txt','utf8'))"})
        assert result.is_error and "OWNED_DENIED_VALUE" not in body(result)

@pytest.mark.asyncio
async def test_modules_survive_reset_while_bindings_are_cleared(tmp_path):
    modules = tmp_path / "additional/node_modules"
    package = modules / "owned-package"
    package.mkdir(parents=True)
    (package / "package.json").write_text('{"type":"module","main":"index.js"}')
    (package / "index.js").write_text('export const value = "ACTUAL_PACKAGE"')
    async with companion(tmp_path) as fixture:
        added = await fixture.client.call_tool("js_add_node_module_dir", {"path": str(modules)})
        assert not added.is_error, body(added)
        first = await fixture.client.call_tool("js", {"code": "let saved=7; const pkg=await import('owned-package'); nodeRepl.write(pkg.value)"})
        assert not first.is_error and body(first) == "ACTUAL_PACKAGE", body(first)
        assert not (await fixture.client.call_tool("js_reset", {})).is_error
        restored = await fixture.client.call_tool("js", {"code": "const pkg=await import('owned-package'); nodeRepl.write(JSON.stringify({saved:typeof saved,value:pkg.value}))"})
        assert not restored.is_error, body(restored)
        assert json.loads(body(restored)) == {"saved": "undefined", "value": "ACTUAL_PACKAGE"}

@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["system", "desktop-bundled"])
async def test_timeout_and_cancel_really_reset_worker_and_next_execution_works(tmp_path, monkeypatch, runtime, request):
    if runtime == "desktop-bundled":
        binary = Path(__file__).resolve().parents[2] / "desktop/node_modules/electron/dist/electron.exe"
        if not binary.is_file():
            pytest.skip("desktop-bundled Node binary is unavailable on this host")
        monkeypatch.setenv("MINICODE_EDITOR_NODE", str(binary))
    async with companion(tmp_path) as fixture:
        timed = await fixture.client.call_tool("js", {"code": "const lost=1; while(true){}", "timeout_ms": 100})
        assert timed.is_error and "kernel was reset" in body(timed), body(timed)
        assert body(await fixture.client.call_tool("js", {"code": "nodeRepl.write(typeof lost)"})) == "undefined"
        cancel = asyncio.Event()
        active = asyncio.create_task(fixture.client.call_tool("js", {"code": "const fs=await import('fs/promises'); const pending=2; await fs.writeFile('cancel-ready','started'); await new Promise(()=>{})"}, request_owner={"cancel_event": cancel}))
        async with asyncio.timeout(3):
            while not (tmp_path / "cancel-ready").exists():
                await asyncio.sleep(0.01)
        cancel.set()
        with pytest.raises(asyncio.CancelledError):
            await active
        # The SDK sends the real cancelled notification; the next request must
        # finish instead of waiting for an abandoned async evaluation.
        observed = await asyncio.wait_for(fixture.client.call_tool("js", {"code": "nodeRepl.write(typeof pending)"}), 3)
        assert not observed.is_error and body(observed) == "undefined", body(observed)
        request.node.user_properties.append(("actual_timeout_cancel", fixture.config.command))


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["system", "desktop-bundled"])
async def test_explicit_reset_interrupts_active_code_and_does_not_run_stale_queue(tmp_path, monkeypatch, runtime, request):
    if runtime == "desktop-bundled":
        binary = Path(__file__).resolve().parents[2] / "desktop/node_modules/electron/dist/electron.exe"
        if not binary.is_file():
            pytest.skip("desktop-bundled Node binary is unavailable on this host")
        monkeypatch.setenv("MINICODE_EDITOR_NODE", str(binary))
    async with companion(tmp_path) as fixture:
        active = asyncio.create_task(fixture.client.call_tool("js", {"code": "const fs=await import('node:fs'); const lost=12; nodeRepl.write('before reset'); fs.writeFileSync('reset-ready','started'); while(true){}"}))
        async with asyncio.timeout(3):
            while not (tmp_path / "reset-ready").exists():
                await asyncio.sleep(0.01)
        queued = asyncio.create_task(fixture.client.call_tool("js", {"code": "const fs=await import('node:fs');fs.writeFileSync('stale-queue','ran')"}))
        await asyncio.sleep(0)
        resets = [asyncio.create_task(fixture.client.call_tool("js_reset", {})) for _ in range(2)]
        reset = await asyncio.wait_for(resets[1], 3)
        assert not reset.is_error, body(reset)
        following = asyncio.create_task(fixture.client.call_tool("js", {"code": "await new Promise(r=>setTimeout(r,40));nodeRepl.write(typeof lost)"}))
        assert not (await resets[0]).is_error
        interrupted, stale = await asyncio.gather(active, queued)
        assert interrupted.is_error and "reset by js_reset" in body(interrupted), body(interrupted)
        assert "before reset" in body(interrupted)
        assert stale.is_error and "queued execution" in body(stale), body(stale)
        assert not (tmp_path / "stale-queue").exists()
        current = await following
        assert not current.is_error and body(current) == "undefined", body(current)
        request.node.user_properties.append(("actual_import_cpu_double_reset", fixture.config.command))


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["system", "desktop-bundled"])
async def test_delayed_outputs_keep_original_request_and_do_not_kill_the_kernel(tmp_path, caplog, request, monkeypatch, runtime):
    if runtime == "desktop-bundled":
        binary = Path(__file__).resolve().parents[2] / "desktop/node_modules/electron/dist/electron.exe"
        if not binary.is_file():
            pytest.skip("desktop-bundled Node binary is unavailable on this host")
        monkeypatch.setenv("MINICODE_EDITOR_NODE", str(binary))
    caplog.set_level("INFO", logger="backend.mcp.client")
    async with companion(tmp_path) as fixture:
        first = await fixture.client.call_tool("js", {"code": "const retained=8;setTimeout(()=>console.log('LATE='+nodeRepl.requestMeta.owner),30);nodeRepl.write('FIRST')"}, request_meta={"owner": "first"})
        assert not first.is_error and body(first) == "FIRST", body(first)
        second = await fixture.client.call_tool("js", {"code": "setTimeout(()=>console.log('IDLE='+nodeRepl.requestMeta.owner),120);await new Promise(r=>setTimeout(r,70));nodeRepl.write('SECOND='+nodeRepl.requestMeta.owner)"}, request_meta={"owner": "second"})
        assert not second.is_error and body(second) == "SECOND=second", body(second)
        await asyncio.sleep(0.12)
        current = await fixture.client.call_tool("js", {"code": "nodeRepl.write(retained)"})
        assert not current.is_error and body(current) == "8", body(current)
        notifications = [record.args[2] for record in caplog.records if record.name == "backend.mcp.client"
            and isinstance(record.args, tuple) and len(record.args) == 3 and isinstance(record.args[2], dict)
            and "requestId" in record.args[2]]
        late = next(item for item in notifications if item["content"][0]["text"] == "LATE=first")
        idle = next(item for item in notifications if item["content"][0]["text"] == "IDLE=second")
        assert late["requestId"] != idle["requestId"]
        assert late["requestMeta"] == {"owner": "first"} and idle["requestMeta"] == {"owner": "second"}
        request.node.user_properties.append(("actual_late_output_notifications", json.dumps(notifications)))
        request.node.user_properties.append(("actual_late_output_executable", fixture.config.command))


@pytest.mark.asyncio
async def test_retiring_manager_reuses_original_process_and_permission_change_has_new_kernel(tmp_path):
    async with companion(tmp_path) as fixture:
        await fixture.client.call_tool("js", {"code": "const original = crypto.randomUUID(); nodeRepl.write(original)"})
        original = body(await fixture.client.call_tool("js", {"code": "nodeRepl.write(original)"}))
        [process] = fixture.backend.process.processes.values()
        pid = process.process.pid
        await fixture.manager.stop_all()
        await fixture.runtime.aclose()
        assert fixture.client.connected and process.process.pid == pid
        runtime = MCPEnvironmentRuntime(fixture.environments, fixture.snapshot, local_cwd=tmp_path.as_uri())
        runtime.node_repl_companions = fixture.owner
        manager = MCPServerManager(config_path=tmp_path / "next.json", workspace_root=tmp_path,
            environment_runtime=runtime, runtime_configs=(fixture.config,))
        try:
            await manager.start_all()
            assert manager.get_client("node_repl") is fixture.client
            assert body(await fixture.client.call_tool("js", {"code": "nodeRepl.write(original)"})) == original
            assert len(fixture.backend.process.processes) == 1
            narrowed = replace(fixture.snapshot.primary(), selection=replace(fixture.snapshot.primary().selection,
                config=EnvironmentConfigState.ready(replace(fixture.snapshot.primary().config, workspace_roots=(tmp_path.parent.as_uri(),)))))
            changed = await fixture.owner.admit(narrowed, None)
            assert changed.source != fixture.config.source
        finally:
            await manager.stop_all()
            await runtime.aclose()

@pytest.mark.asyncio
async def test_real_query_registration_code_mode_images_guardian_and_thread_close(tmp_path, monkeypatch, request, caplog):
    from backend.agent import query_engine
    from backend.agent.guardian.host import GuardianHostState
    from backend.tests import test_code_execution as harness
    from backend.tests.test_source_node_repl_evidence_chain import png
    from backend.ws.execution_context import ConversationThreadRuntime

    backend = LocalExecutionEnvironmentBackend()
    environments = EnvironmentManager(tmp_path, local_backend=backend)
    thread = ConversationThreadRuntime("node-test-store", "code-conv")
    original_run = harness.RunContext
    monkeypatch.setattr(harness, "RunContext", lambda **kwargs: original_run(environment_manager=environments,
        environment_thread_runtime=thread, **kwargs))
    def actual_guardian(*args, **kwargs):
        host = GuardianHostState(*args, **kwargs)
        host.node_repl_review_evidence.enable_image_capture()
        return host
    monkeypatch.setattr(query_engine, "GuardianHostState", actual_guardian)
    image = "data:image/png;base64," + png()
    code = 'const saved=41; nodeRepl.write("ACTUAL_NODE="+saved); await nodeRepl.emitImage({data:' + json.dumps(png()) + ',mimeType:"image/png",detail:"original"})'
    model = harness.ScriptModel('const r=await tools.mcp__node_repl__js(' + json.dumps({"code": code}) + '); text(r.content[0].text); image(r.content[1]);')
    approvals = []
    async def approve(call_id):
        approvals.append(call_id)
        return {"action": "approve"}
    try:
        state, builder, _, events, run = await asyncio.create_task(harness.run_model(tmp_path, model, approval=approve))
        assert state.terminal_status == "completed", [event.data for event in events if event.type == "error"]
        receipt = next(item for item in state.tool_calls if item.tool_name == "mcp__node_repl__js")
        assert receipt.status == "success", receipt.result_payload
        assert receipt.call_source["kind"] == "code_mode"
        evidence = run.node_repl_review_evidence.snapshot_since(0)
        assert evidence is not None
        assert any(image_item["image_url"] == image and image_item["detail"] == "original" for image_item in evidence.images())
        assert any("ACTUAL_NODE=41" in getattr(item, "text", "") for response in evidence.responses for item in response.items)
        native = next(message.code_mode_output for message in model.inputs[-1] if message.role == "tool" and message.code_mode_output is not None)
        assert any(item["type"] == "input_image" and item["image_url"] == image for item in native)
        [process] = backend.process.processes.values()
        pid = process.process.pid
        second = harness.ScriptModel('const r=await tools.mcp__node_repl__js({code:"const saved=42; nodeRepl.write(saved)"}); text(r.content[0].text);')
        state2, _, _, _, run2 = await asyncio.create_task(harness.run_model(tmp_path, second, approval=approve))
        assert state2.terminal_status == "completed"
        assert "42" in json.dumps(second.reports)
        assert len(backend.process.processes) == 1 and process.process.pid == pid
        assert run.environment_host_captured and run.environment_host_mcp_manager is None
        assert run2.environment_host_captured and run2.environment_host_mcp_manager is None
        assert not any(record.name == "backend.mcp.manager" and "Failed to start MCP server" in record.getMessage()
            for record in caplog.records)
        request.node.user_properties.append(("actual_node_lifecycle", json.dumps({"pid": pid, "mcp_tool": receipt.tool_name,
            "source": receipt.call_source, "same_process_across_turns": True, "guardian_images": len(evidence.images()),
            "provider_image": True, "process_sandbox": process.sandbox, "approval_count": len(approvals)})))
    finally:
        await thread.close_environments()
        assert not backend.process.processes
        await environments.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("allowed_server", "model_disabled"), [(None, False), ("other-server", False), ("node_repl", False), ("node_repl", True)])
async def test_real_builtin_registration_obeys_captured_environment_mcp_authority(tmp_path, monkeypatch, allowed_server, model_disabled):
    import shutil
    from backend.agent.run_context import RunContext
    from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
    from backend.mcp.environment_runtime import PreparedEnvironmentCapabilities
    from backend.mcp.node_repl import register_node_companion
    from backend.mcp.registry import MCPToolRegistry
    from backend.tools.registry import ToolRegistry

    node = shutil.which("node")
    assert node is not None
    monkeypatch.setenv("MINICODE_EDITOR_NODE", node)
    servers = {} if allowed_server is None else {allowed_server: {"identity": {"command": node}}}
    config = EnvironmentConfig(PermissionProfile.disabled(), (tmp_path.as_uri(),), mcp_policy={"servers": servers})
    backend = LocalExecutionEnvironmentBackend()
    environments = EnvironmentManager(tmp_path, local_backend=backend)
    async def resolve(selection, config, environment, info):
        return config
    thread = ThreadEnvironments(environments, ThreadEnvironmentDefaults(config, "disabled"), resolve_config=resolve)
    await thread.update_selections([TurnEnvironmentSelection.from_request(TurnEnvironmentRequest("local", tmp_path.as_uri(), (tmp_path.as_uri(),), EnvironmentConfigState.ready(config)))])
    snapshot = await thread.snapshot()
    runtime = MCPEnvironmentRuntime(environments, snapshot, local_cwd=tmp_path.as_uri())
    manager = MCPServerManager(config_path=tmp_path / "authority.json", workspace_root=tmp_path,
        environment_runtime=runtime, runtime_configs=())
    registry = ToolRegistry()
    MCPToolRegistry(registry, mcp_manager=manager)
    host = SimpleNamespace(node_repl_companions=None)
    prepared = PreparedEnvironmentCapabilities(manager, None, (), (), runtime, True, "authority", "turn")
    run = RunContext(turn_environments=snapshot, environment_capabilities=prepared, environment_thread_runtime=host,
        network_step=SimpleNamespace(policy_for=lambda attachment: None),
        model_execution=SimpleNamespace(config=SimpleNamespace(config_layer_stack=ConfigLayerStack(())), model_info=SimpleNamespace(extra={"node_repl_disabled": model_disabled})),
        active_model_execution=SimpleNamespace(config=SimpleNamespace(config_layer_stack=ConfigLayerStack((
            ConfigLayer(ConfigLayerSource("policy"), {"denied_mcp_servers": [{"name": "node_repl"}]}),))),
            model_info=SimpleNamespace(extra={"node_repl_disabled": not model_disabled})))
    try:
        await register_node_companion(run, registry, None, execution=run.model_execution)
        if allowed_server == "node_repl" and not model_disabled:
            client = manager.get_client("node_repl")
            assert client is not None and client.connected, manager.get_all_status()
            assert len(backend.process.processes) == 1
            assert registry.get_tool("mcp__node_repl__js") is not None
            result = await client.call_tool("js", {"code": "nodeRepl.write('authority admitted')"})
            assert not result.is_error and body(result) == "authority admitted", body(result)
        else:
            assert not manager.get_server_config("node_repl").enabled
            assert manager.get_client("node_repl") is None
            assert not backend.process.processes
            assert registry.get_tool("mcp__node_repl__js") is None
    finally:
        await manager.stop_all()
        if host.node_repl_companions is not None:
            await host.node_repl_companions.aclose()
        await runtime.aclose()
        await snapshot.aclose()
        await thread.aclose()
        assert not backend.process.processes
        await environments.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_change", ["filesystem", "mcp-authority"])
async def test_scope_retirement_waits_for_original_call_then_stops_its_timer(tmp_path, scope_change):
    from backend.agent.run_context import RunContext
    from backend.mcp.environment_runtime import PreparedEnvironmentCapabilities

    async with companion(tmp_path) as fixture:
        old = fixture.client
        running = asyncio.create_task(old.call_tool("js", {"code": "const fs=await import('node:fs'); const timer=setInterval(()=>fs.appendFileSync('old-timer','.'),10); fs.writeFileSync('old-ready','ready'); const oldState=21; await new Promise(r=>setTimeout(r,200)); nodeRepl.write(oldState);"}))
        async with asyncio.timeout(3):
            while not (tmp_path / "old-ready").exists():
                await asyncio.sleep(0.01)
        original = fixture.snapshot.primary()
        changed_config = (replace(original.config, workspace_roots=(tmp_path.parent.as_uri(),)) if scope_change == "filesystem"
            else replace(original.config, mcp_policy={"servers": {}}))
        moved = replace(original, selection=replace(original.selection, config=EnvironmentConfigState.ready(changed_config)))
        await fixture.thread.update_selections([moved.selection])
        changed_snapshot = await fixture.thread.snapshot()
        changed = await fixture.owner.admit(changed_snapshot.primary(), None)
        assert changed.source != fixture.config.source and old.connected
        retiring = PreparedEnvironmentCapabilities(fixture.manager, None, (), (), fixture.runtime, True, "conversation", "turn").retire(RunContext())
        observed = await running
        assert not observed.is_error and body(observed) == "21"
        await retiring
        assert not old.connected and not fixture.backend.process.processes
        size = (tmp_path / "old-timer").stat().st_size
        await asyncio.sleep(0.04)
        assert (tmp_path / "old-timer").stat().st_size == size
        runtime = MCPEnvironmentRuntime(fixture.environments, changed_snapshot, local_cwd=tmp_path.as_uri())
        runtime.node_repl_companions = fixture.owner
        manager = MCPServerManager(config_path=tmp_path / "changed.json", workspace_root=tmp_path,
            environment_runtime=runtime, runtime_configs=(changed,))
        try:
            await manager.start_all()
            current = manager.get_client("node_repl")
            if scope_change == "mcp-authority":
                assert current is None and not fixture.backend.process.processes
            else:
                assert current is not None and current is not old
                result = await current.call_tool("js", {"code": "nodeRepl.write(typeof oldState)"})
                assert not result.is_error and body(result) == "undefined"
        finally:
            await manager.stop_all()
            await runtime.aclose()
            await changed_snapshot.aclose()

@pytest.mark.asyncio
@pytest.mark.parametrize("is_subagent", [False, True])
async def test_prepared_capabilities_re_admit_node_after_scope_and_preserve_user_catalog(tmp_path, is_subagent, caplog, request):
    from backend.agent.run_context import RunContext
    from backend.artifact.store import ArtifactStore
    from backend.config_layers import ConfigLayerStack
    from backend.feature_flags import FeatureFlags
    from backend.mcp.environment_runtime import prepare_environment_capabilities
    from backend.mcp.node_repl import register_node_companion
    from backend.tools.registry import ToolRegistry

    async with companion(tmp_path) as fixture:
        await fixture.client.call_tool("js", {"code":"const parentState=7"})
        user_config = replace(fixture.config, name="configured_tools", source="user", cwd=str(tmp_path))
        fixture.manager._runtime_configs += (user_config,)
        await fixture.manager.start_server(user_config)
        prepared = await prepare_environment_capabilities(turn_environments=fixture.snapshot,
            host_skill_manager=None, host_mcp_manager=fixture.manager, feature_flags=FeatureFlags({}),
            thread_id="receiving", turn_id="next", session_source="subagent" if is_subagent else "sdk",
            isolated=True, is_subagent=is_subagent, environment_manager=fixture.environments, local_cwd=tmp_path.as_uri())
        store = ArtifactStore(storage_dir=tmp_path / "prepared-artifacts")
        host = SimpleNamespace(node_repl_companions=None if is_subagent else fixture.owner)
        try:
            assert prepared.mcp_manager.get_server_config("node_repl") is None
            assert "node_repl" not in prepared.mcp_manager._cached_catalog
            assert "node_repl" not in prepared.mcp_manager.get_all_tools()
            assert prepared.mcp_manager.get_server_config("configured_tools").source == "user"
            if is_subagent:
                assert prepared.mcp_manager.get_client("configured_tools") is None
                assert "configured_tools" in prepared.mcp_manager._lazy_servers
                assert prepared.mcp_manager.get_all_tools()["configured_tools"]
            else:
                assert prepared.mcp_manager.get_client("configured_tools") is not None
            registry = ToolRegistry()
            prepared.bind_registry(registry, store)
            run = RunContext(turn_environments=fixture.snapshot, environment_capabilities=prepared,
                environment_thread_runtime=host, network_step=SimpleNamespace(policy_for=lambda attachment:None))
            execution = SimpleNamespace(config=SimpleNamespace(config_layer_stack=ConfigLayerStack(())), model_info=None)
            await register_node_companion(run, registry, None, execution=execution)
            current = prepared.mcp_manager.get_client("node_repl")
            assert current is not None
            if is_subagent:
                assert current is not fixture.client and host.node_repl_companions is not fixture.owner
                result = await current.call_tool("js", {"code":"nodeRepl.write(typeof parentState)"})
                assert not result.is_error and body(result) == "undefined", body(result)
                await prepared.mcp_manager.prepare_server_call("configured_tools")
                assert prepared.mcp_manager.get_client("configured_tools") is not None
            else:
                assert current is fixture.client
            assert not any(record.name == "backend.mcp.manager" and "Failed to start MCP server" in record.getMessage()
                for record in caplog.records)
            request.node.user_properties.append(("actual_prepared_node_admission", json.dumps({
                "subagent":is_subagent,"old_builtin_config_and_cache_not_inherited":True,
                "user_catalog_retained":True,"child_lazy_until_first_call":is_subagent,
                "node_owner_independent_for_child":is_subagent,"no_pre_admission_startup_errors":True})))
        finally:
            await prepared.aclose()
            if host.node_repl_companions is not None and host.node_repl_companions is not fixture.owner:
                await host.node_repl_companions.aclose()
            store.shutdown()


@pytest.mark.asyncio
async def test_configured_same_name_override_retires_original_builtin_after_its_borrower(tmp_path):
    from backend.agent.run_context import RunContext
    from backend.mcp.client import MCPClient
    from backend.mcp.environment_runtime import PreparedEnvironmentCapabilities
    from backend.mcp.node_repl import register_node_companion
    from backend.mcp.registry import MCPToolRegistry
    from backend.tools.registry import ToolRegistry
    from backend.feature_flags import FeatureFlags
    from backend.mcp.environment_runtime import prepare_environment_capabilities

    async with companion(tmp_path) as fixture:
        old = fixture.client
        original_handle = old.process_handle
        active = asyncio.create_task(old.call_tool("js", {"code": "const fs=await import('node:fs');const timer=setInterval(()=>fs.appendFileSync('override-timer','.'),10);fs.writeFileSync('override-ready','ready');while(!fs.existsSync('override-release'))await new Promise(r=>setTimeout(r,10));nodeRepl.write('original completed')"}))
        async with asyncio.timeout(3):
            while not (tmp_path / "override-ready").exists():
                await asyncio.sleep(0.01)
        runtime = MCPEnvironmentRuntime(fixture.environments, fixture.snapshot, local_cwd=tmp_path.as_uri())
        override = replace(fixture.config, source="user:explicit-node-repl", cwd=str(tmp_path))
        manager = MCPServerManager(config_path=tmp_path / "override.json", workspace_root=tmp_path,
            environment_runtime=runtime, runtime_configs=(override,))
        registry = ToolRegistry()
        MCPToolRegistry(registry, mcp_manager=manager)
        try:
            await manager.start_all()
            registry.mcp_tool_registry.sync()
            configured = manager.get_client("node_repl")
            assert type(configured) is MCPClient and configured is not old, manager.get_all_status()
            inherited = await prepare_environment_capabilities(turn_environments=fixture.snapshot,
                host_skill_manager=None, host_mcp_manager=manager, feature_flags=FeatureFlags({}),
                thread_id="user-override-child", turn_id="next", session_source="subagent", is_subagent=True,
                isolated=True, environment_manager=fixture.environments, local_cwd=tmp_path.as_uri())
            try:
                assert inherited.mcp_manager.get_server_config("node_repl").source == "user:explicit-node-repl"
                assert "node_repl" in inherited.mcp_manager._lazy_servers
                assert inherited.mcp_manager.get_all_tools()["node_repl"]
            finally:
                await inherited.aclose()
            prepared = PreparedEnvironmentCapabilities(manager, None, (), (), runtime, True, "conversation", "override")
            run = RunContext(turn_environments=fixture.snapshot, environment_capabilities=prepared,
                environment_thread_runtime=SimpleNamespace(node_repl_companions=fixture.owner))
            await register_node_companion(run, registry, None, execution=None)
            assert old.connected and next(iter(fixture.owner.instances.values())).retired
            retiring = PreparedEnvironmentCapabilities(fixture.manager, None, (), (), fixture.runtime, True, "conversation", "old").retire(RunContext())
            await asyncio.sleep(0)
            assert not active.done() and not retiring.done()
            (tmp_path / "override-release").write_text("released")
            observed = await active
            assert not observed.is_error and body(observed) == "original completed", body(observed)
            await retiring
            assert original_handle.closed and not old.connected
            size = (tmp_path / "override-timer").stat().st_size
            await asyncio.sleep(0.04)
            assert (tmp_path / "override-timer").stat().st_size == size
            assert manager.get_client("node_repl") is configured and configured.connected
            assert registry.get_tool("mcp__node_repl__js") is not None
            result = await configured.call_tool("js", {"code": "nodeRepl.write('configured server remains')"})
            assert not result.is_error and body(result) == "configured server remains", body(result)
        finally:
            await manager.stop_all()
            await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32", reason="actual Windows native sandbox account rejection")
async def test_elevated_account_rejected_node_start_closes_original_handle_and_thread(tmp_path, monkeypatch):
    from backend.sandbox.policy import FileSystemSandboxPolicy, FileSystemSandboxEntry, FileSystemPath, FileSystemSpecialPath, FileSystemAccessMode, NetworkSandboxPolicy
    from backend.sandbox import windows_native

    missing_home = tmp_path / "missing-owned-native-account-state"
    monkeypatch.setenv(windows_native._HOME_ENV, str(missing_home))
    profile = PermissionProfile.managed(FileSystemSandboxPolicy.restricted([
        FileSystemSandboxEntry(FileSystemPath.special(FileSystemSpecialPath.ROOT), FileSystemAccessMode.READ),
        FileSystemSandboxEntry(FileSystemPath("path", tmp_path.as_uri()), FileSystemAccessMode.WRITE),
    ]), network=NetworkSandboxPolicy.RESTRICTED)
    config = EnvironmentConfig(profile, (tmp_path.as_uri(),), windows_sandbox_type="windowsRestrictedToken", windows_sandbox_level="elevated")
    backend = LocalExecutionEnvironmentBackend()
    environments = EnvironmentManager(tmp_path, local_backend=backend)
    async def resolve(selection, config, environment, info):
        return config
    thread = ThreadEnvironments(environments, ThreadEnvironmentDefaults(config, "disabled"), resolve_config=resolve)
    await thread.update_selections([TurnEnvironmentSelection.from_request(TurnEnvironmentRequest("local", tmp_path.as_uri(), (tmp_path.as_uri(),), EnvironmentConfigState.ready(config)))])
    snapshot = await thread.snapshot()
    owner = NodeReplCompanions()
    runtime = MCPEnvironmentRuntime(environments, snapshot, local_cwd=tmp_path.as_uri())
    runtime.node_repl_companions = owner
    registered = await owner.admit(snapshot.primary(), None)
    manager = MCPServerManager(config_path=tmp_path / "native-rejected.json", workspace_root=tmp_path,
        environment_runtime=runtime, runtime_configs=(registered,))
    try:
        await manager.start_all()
        [companion] = owner.instances.values()
        handle = companion.client.process_handle
        assert manager.get_client("node_repl") is None
        assert "native sandbox account state is unavailable" in manager.get_all_status()[0]["error"]
        assert handle is not None and not handle.accepted and handle.process is None and handle.watch_task is None
        assert (await handle.aclose())["closed"] and handle.closed and handle.failure is not None
        assert not backend.process.processes and not missing_home.exists()
    finally:
        await manager.stop_all()
        await owner.aclose()
        await runtime.aclose()
        await snapshot.aclose()
        await thread.aclose()
        await environments.close()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32", reason="actual Windows restricted-token Node startup")
async def test_restricted_node_starts_without_elevated_accounts_and_keeps_original_kernel(monkeypatch):
    import os
    import tempfile
    from backend.sandbox import windows_native
    from backend.sandbox.policy import FileSystemSandboxPolicy, FileSystemSandboxEntry, FileSystemPath, FileSystemSpecialPath, FileSystemAccessMode, NetworkSandboxPolicy

    executable = Path(__file__).resolve().parents[2] / "desktop/windows-sandbox-runtime/codex.exe"
    if not executable.is_file():
        pytest.skip("Bundled Windows sandbox runtime has not been built")
    arena = Path(os.environ.get("MINICODE_SANDBOX_TEST_ROOT", "C:/minicode-sbx-test"))
    arena.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="node-readiness-", dir=arena) as directory:
        root = Path(directory)
        workspace, home = root / "workspace", root / "home"
        workspace.mkdir()
        monkeypatch.setenv(windows_native._RUNNER_ENV, str(executable))
        monkeypatch.setenv(windows_native._HOME_ENV, str(home))
        monkeypatch.setattr(windows_native, "discover_runtime", lambda: pytest.fail("Node restricted-token must not require elevated setup"))
        profile = PermissionProfile.managed(FileSystemSandboxPolicy.restricted([
            FileSystemSandboxEntry(FileSystemPath.special(FileSystemSpecialPath.ROOT), FileSystemAccessMode.READ),
            # Ready environment profiles already use the executor URI domain;
            # only the selected local backend converts this to a native path.
            FileSystemSandboxEntry(FileSystemPath("path", workspace.as_uri()), FileSystemAccessMode.WRITE),
        ]), network=NetworkSandboxPolicy.RESTRICTED)
        config = EnvironmentConfig(profile, (workspace.as_uri(),), windows_sandbox_type="windowsRestrictedToken", windows_sandbox_level="restricted-token")
        async with companion(workspace, environment_config=config) as fixture:
            handle = fixture.client.process_handle
            assert handle.accepted and handle.sandbox_backend == "windows-restricted-token"
            pid = handle.process.pid
            first = await fixture.client.call_tool("js", {"code": "const fs=await import('node:fs/promises');const saved=41;await fs.writeFile('inside.txt','allowed');nodeRepl.write(saved)"})
            assert not first.is_error and body(first) == "41", body(first)
            assert (workspace / "inside.txt").read_text() == "allowed"
            second = await fixture.client.call_tool("js", {"code": "nodeRepl.write(saved+1)"})
            assert not second.is_error and body(second) == "42", body(second)
            assert fixture.client.process_handle is handle and handle.process.pid == pid
            outside = root / "outside.txt"
            denied = await fixture.client.call_tool("js", {"code": f"await fs.writeFile({json.dumps(str(outside))},'blocked')"})
            assert denied.is_error and not outside.exists()
        assert handle.closed and not fixture.backend.process.processes
        assert not (home / ".sandbox-secrets/sandbox_users.json").exists()
        assert not (home / ".sandbox/setup_marker.json").exists()


@pytest.mark.asyncio
async def test_same_id_executor_replacement_uses_new_node_process_and_releases_original(tmp_path):
    import sys
    from backend.tests.test_environments import configure, PEER
    from backend.tests.test_remote_mcp_skill_runtime import _capture

    configure(tmp_path, [{"id": "remote", "program": sys.executable, "args": ["-u", str(PEER)]}], default="remote", include_local=False)
    environments = EnvironmentManager(tmp_path)
    thread, snapshot = await _capture(environments, tmp_path.as_uri())
    original_environment = snapshot.primary().environment
    owner = NodeReplCompanions()
    config = await owner.admit(snapshot.primary(), None)
    runtime = MCPEnvironmentRuntime(environments, snapshot, local_cwd=tmp_path.as_uri())
    runtime.node_repl_companions = owner
    first = MCPServerManager(config_path=tmp_path / "first.json", workspace_root=tmp_path,
        environment_runtime=runtime, runtime_configs=(config,))
    second, next_runtime, next_snapshot = None, None, None
    try:
        await first.start_all()
        old = first.get_client("node_repl")
        assert old is not None, first.get_all_status()
        assert not (await old.call_tool("js", {"code": "const identity='original'; nodeRepl.write(identity)"})).is_error
        original_handle = old.process_handle
        environments.upsert({"id": "remote", "program": sys.executable, "args": ["-u", str(PEER)], "env": {"NODE_INSTANCE": "replacement"}})
        await thread.update_selections([snapshot.primary().selection])
        pinned = await thread.snapshot()
        assert pinned.primary().environment is original_environment
        await pinned.aclose()
        # Source selections retain their original registered object. Only a
        # real detach/reattach receives the replacement with the same ID.
        await thread.update_selections([])
        await thread.update_selections([snapshot.primary().selection])
        next_snapshot = await thread.snapshot()
        assert next_snapshot.primary().environment is not original_environment
        next_config = await owner.admit(next_snapshot.primary(), None)
        await first.stop_all()
        await runtime.aclose()
        await snapshot.aclose()
        assert original_handle.closed and not old.connected
        next_runtime = MCPEnvironmentRuntime(environments, next_snapshot, local_cwd=tmp_path.as_uri())
        next_runtime.node_repl_companions = owner
        second = MCPServerManager(config_path=tmp_path / "second.json", workspace_root=tmp_path,
            environment_runtime=next_runtime, runtime_configs=(next_config,))
        await second.start_all()
        current = second.get_client("node_repl")
        assert current is not None and current is not old, second.get_all_status()
        result = await current.call_tool("js", {"code": "nodeRepl.write(typeof identity)"})
        assert not result.is_error and body(result) == "undefined"
        assert not original_environment.client.processes
    finally:
        await first.stop_all()
        if second is not None:
            await second.stop_all()
        await owner.aclose()
        await runtime.aclose()
        if next_runtime is not None:
            await next_runtime.aclose()
        await snapshot.aclose()
        if next_snapshot is not None:
            await next_snapshot.aclose()
        await thread.aclose()
        assert not any(environment.client.processes for environment in [original_environment, environments.get_environment("remote")])
        await environments.close()
