"""Independent CUA protocol/receiving ownership; native actions are controlled fixtures."""
from contextlib import asynccontextmanager
import asyncio
from dataclasses import replace
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest
import psutil

from backend.mcp.cua_repl import CuaReplCompanions, CuaTurn, is_configured_cua
from backend.mcp.manager import MCPServerConfig, MCPServerManager
from backend.mcp.native_cua_runtime import PROVIDER, build_native_bootstrap, native_cua_config
from backend.mcp.native_cua_windows import NativeCuaSession, WindowsDesktop, INPUT, MOUSEINPUT, KEYBDINPUT
from backend.tests.fixtures.native_cua_receiving import FixtureDesktop
from backend.tests.test_sdk_session_ownership_chain import sdk_owner


OWNER = {"session_id": "real-thread", "turn_id": "real-turn"}


def test_original_window_target_requires_app_approval_and_exact_turn():
    desktop = FixtureDesktop()
    session = NativeCuaSession(desktop)
    selected = session.dispatch("select_window", {"window_id": "11", "target": "first-target"}, OWNER)
    args = {"target": selected["target"]}
    with pytest.raises(PermissionError, match="not been approved"):
        session.dispatch("screenshot", args, OWNER)
    session.dispatch("authorize", args, OWNER)
    assert session.dispatch("screenshot", args, OWNER)["width"] == 1
    with pytest.raises(PermissionError, match="another thread or turn"):
        session.dispatch("click", {**args, "x": 2, "y": 3}, {**OWNER, "turn_id": "foreign"})
    assert not session.dispatch("end_turn", {}, {**OWNER, "turn_id": "foreign"})
    desktop.pins.clear()  # Same HWND and process reused after the original window dies.
    with pytest.raises(RuntimeError, match="destroyed or replaced"):
        session.dispatch("click", {**args, "x": 2, "y": 3}, OWNER)
    assert desktop.operations == [("fixture_capture", 11)]
    session.close()


def test_process_replacement_is_not_authorized_and_original_turn_close_keeps_new_turn():
    desktop = FixtureDesktop()
    session = NativeCuaSession(desktop)
    first = session.dispatch("select_window", {"window_id": "11", "target": "first-target"}, OWNER)
    session.dispatch("authorize", {"target": first["target"]}, OWNER)
    desktop.current_identity["created"] = 124
    with pytest.raises(RuntimeError, match="process identity has changed"):
        session.dispatch("get_state", {"target": first["target"]}, OWNER)
    next_owner = {**OWNER, "turn_id": "next-turn"}
    second = session.dispatch("select_window", {"window_id": "11", "target": "second-target"}, next_owner)
    assert session.dispatch("end_turn", {}, OWNER)
    assert set(session.targets) == {second["target"]}
    session.close()
    assert not desktop.pins


def test_native_input_abi_and_unicode_pairs_are_real_win32_shapes_without_calling_windows():
    assert ctypes.sizeof(INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
    assert ctypes.sizeof(MOUSEINPUT) == (32 if ctypes.sizeof(ctypes.c_void_p) == 8 else 24)
    assert ctypes.sizeof(KEYBDINPUT) == (24 if ctypes.sizeof(ctypes.c_void_p) == 8 else 16)
    desktop = object.__new__(WindowsDesktop)
    captured = []
    desktop.send = lambda hwnd, events, check_target: captured.extend(events)
    desktop.input(11, "type_text", {"text": "中😀"}, lambda: None)
    assert [(item.ki.wScan, item.ki.dwFlags) for item in captured] == [
        (0x4E2D, 4), (0x4E2D, 6), (0xD83D, 4), (0xD83D, 6), (0xDE00, 4), (0xDE00, 6)]
    captured.clear()
    desktop.input(11, "keypress", {"keys": ["CTRL", "L"]}, lambda: None)
    assert [(item.ki.wVk, item.ki.dwFlags) for item in captured] == [(17, 0), (76, 0), (76, 2), (17, 2)]


def test_capture_rechecks_original_window_before_returning_any_image():
    desktop = FixtureDesktop()
    session = NativeCuaSession(desktop)
    args = {"window_id": "11", "target": "captured-target"}
    session.dispatch("select_window", args, OWNER)
    session.dispatch("authorize", args, OWNER)
    original_capture = desktop.capture
    def replaced_during_capture(hwnd):
        image = original_capture(hwnd)
        desktop.pins.clear()
        return image
    desktop.capture = replaced_during_capture
    with pytest.raises(RuntimeError, match="destroyed or replaced"):
        session.dispatch("screenshot", args, OWNER)
    session.close()


def test_input_checks_original_target_after_focus_before_sendinput():
    desktop = object.__new__(WindowsDesktop)
    focused, sent = [], []
    desktop.focus = lambda hwnd: focused.append(hwnd)
    desktop.user = SimpleNamespace(SendInput=lambda *args: sent.append(args))
    def replaced_after_focus():
        assert focused == [11]
        raise RuntimeError("original target replaced")
    with pytest.raises(RuntimeError, match="original target replaced"):
        desktop.send(11, [INPUT()], replaced_after_focus)
    assert sent == []


def test_global_input_does_not_claim_atomic_targeting_when_foreground_changes():
    desktop = object.__new__(WindowsDesktop)
    sent = []
    desktop.focus = lambda hwnd: None
    desktop.user = SimpleNamespace(GetForegroundWindow=lambda: 22, SendInput=lambda *args: sent.append(args))
    with pytest.raises(RuntimeError, match="Foreground changed"):
        desktop.send(11, [INPUT()], lambda: None)
    assert sent == []


@pytest.mark.parametrize("name", ["ApplicationFrameHost.exe", "WWAHost.exe"])
def test_hosted_window_cannot_inherit_a_shared_executable_app_grant(monkeypatch, name):
    desktop = object.__new__(WindowsDesktop)
    desktop.user = SimpleNamespace(IsWindow=lambda hwnd: True,
        GetWindowThreadProcessId=lambda hwnd, pid: setattr(pid._obj, "value", 91))
    monkeypatch.setattr(psutil, "Process", lambda pid: SimpleNamespace(exe=lambda: "C:\\Windows\\" + name,
        create_time=lambda: 123, name=lambda: name))
    with pytest.raises(RuntimeError, match="per-window AUMID"):
        desktop.identity(11)


def test_explicit_native_selection_uses_receiving_python_and_never_installed_runtime(tmp_path):
    selected = MCPServerConfig("cua_repl", command="receiving-node", env={
        "MINICODE_CUA_PROVIDER": PROVIDER, "CUA_REPL_ENABLED_SURFACES": "computer",
        "MINICODE_CUA_PYTHON": "D:\\receiving\\python.exe"})
    attachment = SimpleNamespace(is_remote=True, peer_info={"platformOs": "windows"})
    assert is_configured_cua(selected)
    configured = native_cua_config(selected, attachment, {})
    assert configured.command == "receiving-node" and configured.args[:3] == ["--expose-internals", "--input-type=module", "--eval"]
    assert "ELECTRON_RUN_AS_NODE" not in configured.env
    assert len(subprocess.list2cmdline([configured.command, *configured.args])) < 32767
    with pytest.raises(KeyError, match="MINICODE_CUA_PYTHON"):
        native_cua_config(replace(selected, env={"MINICODE_CUA_PROVIDER": PROVIDER, "CUA_REPL_ENABLED_SURFACES": "computer"}), attachment, {})
    with pytest.raises(ValueError, match="absolute Python path"):
        native_cua_config(replace(selected, env={**selected.env, "MINICODE_CUA_PYTHON": "python"}), attachment, {})
    with pytest.raises(ValueError, match="no macOS/Linux adapter"):
        native_cua_config(selected, SimpleNamespace(is_remote=True, peer_info={"platformOs": "linux"}), {})
    with pytest.raises(ValueError, match="Unknown MINICODE_CUA_PROVIDER"):
        is_configured_cua(replace(selected, env={**selected.env, "MINICODE_CUA_PROVIDER": "unknown"}))


@asynccontextmanager
async def native_companion(tmp_path, monkeypatch, *, choice, app_access=None, blocked=False):
    from backend.agent.environment_selection import EnvironmentConfig, EnvironmentConfigState, ThreadEnvironmentDefaults, ThreadEnvironments, TurnEnvironmentRequest, TurnEnvironmentSelection
    from backend.environments import EnvironmentManager
    from backend.mcp.environment_runtime import MCPEnvironmentRuntime
    from backend.sandbox.policy import PermissionProfile
    from backend.workspace.executor_local import LocalExecutionEnvironmentBackend
    import backend.mcp.native_cua_runtime as module

    backend = LocalExecutionEnvironmentBackend()
    environments = EnvironmentManager(tmp_path, local_backend=backend)
    config = EnvironmentConfig(PermissionProfile.disabled(), (tmp_path.as_uri(),))
    async def resolve(selection, config, environment, info):
        return config
    thread = ThreadEnvironments(environments, ThreadEnvironmentDefaults(config, "disabled"), resolve_config=resolve)
    await thread.update_selections([TurnEnvironmentSelection.from_request(TurnEnvironmentRequest("local", tmp_path.as_uri(),
        (tmp_path.as_uri(),), EnvironmentConfigState.ready(config)))])
    snapshot = await thread.snapshot()
    owner = CuaReplCompanions()
    runtime = MCPEnvironmentRuntime(environments, snapshot, local_cwd=tmp_path.as_uri())
    runtime.cua_repl_companions = owner
    fixture = Path(__file__).parent / "fixtures/native_cua_receiving.py"
    source = "import sys;sys.path.insert(0," + repr(str(Path(__file__).parents[2])) + ")\n" + fixture.read_text(encoding="utf-8")
    real_builder = module.build_native_bootstrap
    monkeypatch.setattr(module, "build_native_bootstrap", lambda **kwargs: real_builder(**kwargs, native_source=source))
    log = tmp_path / "native-fixture.jsonl"
    selected = MCPServerConfig("cua_repl", command=shutil.which("node"), env={
        "MINICODE_CUA_PROVIDER": PROVIDER, "CUA_REPL_ENABLED_SURFACES": "computer", "MINICODE_CUA_FIXTURE_LOG": str(log)})
    if blocked:
        selected = replace(selected, env={**selected.env, "MINICODE_CUA_FIXTURE_BLOCK": "1"})
    attachment = snapshot.primary()
    # This is a protocol fixture, not a claim of native Windows execution on CI Linux.
    attachment = replace(attachment, peer_info={**attachment.peer_info, "platformOs": "windows"})
    registered = await owner.admit(attachment, None, selected, app_access=app_access)
    approvals = []
    async def approve(params):
        approvals.append(params)
        return choice
    manager = MCPServerManager(config_path=tmp_path / "mcp.json", workspace_root=tmp_path,
        environment_runtime=runtime, runtime_configs=(registered,), elicitation_handler=approve)
    try:
        await manager.start_all()
        client = manager.get_client("cua_repl")
        assert client is not None, manager.get_all_status()
        yield SimpleNamespace(client=client, owner=owner, approvals=approvals, log=log, config=registered)
    finally:
        await manager.stop_all()
        await owner.aclose()
        await runtime.aclose()
        await snapshot.aclose()
        await thread.aclose()
        assert not backend.process.processes
        await environments.close()


def text(result):
    return "\n".join(item["text"] for item in result.content if item["type"] == "text")


@pytest.mark.asyncio
async def test_real_mcp_approval_original_owner_images_turn_retirement_and_thread_close(tmp_path, monkeypatch):
    async with native_companion(tmp_path, monkeypatch, choice={"action": "accept", "content": {"persist": "session"}}) as case:
        tools = await case.client.list_tools()
        assert {tool.name for tool in tools} == {"js", "js_reset", "js_add_node_module_dir"}
        assert "Independent Windows" in case.client.instructions
        request_owner = {"session_id": "receiving-session", "conversation_id": OWNER["session_id"], "turn_id": OWNER["turn_id"], "message_id": "actual-message"}
        first = await case.client.call_tool("js", {"code": "var app = await cua.getApp({window_id:'11'}); await app.screenshot(); await app.type({text:'fixture input'});"}, request_owner=request_owner,
            request_meta={"x-codex-turn-metadata": {"session_id": "foreign", "turn_id": "foreign"}})
        assert not first.is_error, text(first)
        assert len([item for item in first.content if item["type"] == "image"]) == 1
        assert len(case.approvals) == 1
        approval = case.approvals[0]
        assert approval["_minicode_owner"] == request_owner
        assert approval["_meta"]["connector_id"] == "computer-use" and approval["_meta"]["persist"] == ["session"]
        assert await case.client.host.request("closeActiveTurn", {"sessionId": "foreign", "turnId": "foreign"}) is False
        await CuaTurn(SimpleNamespace(client=case.client), OWNER["session_id"], OWNER["turn_id"]).finish_after(())
        stale = await case.client.call_tool("js", {"code": "await app.getState()"}, request_owner=request_owner)
        assert stale.is_error
        next_owner = {**request_owner, "turn_id": "next-turn"}
        resumed = await case.client.call_tool("js", {"code": "var app = await cua.getApp({window_id:'11'}); await app.keypress({keys:['CTRL','L']});"}, request_owner=next_owner)
        assert not resumed.is_error, text(resumed)
        assert len(case.approvals) == 1
        reset = await case.client.call_tool("js_reset")
        assert not reset.is_error
        rows = [json.loads(line) for line in case.log.read_text().splitlines()]
        assert {row["owner"]["session_id"] for row in rows if row.get("method") not in {None, "end_turn"}} == {OWNER["session_id"]}
        assert {row["owner"]["turn_id"] for row in rows if row.get("method") == "keypress"} == {"next-turn"}
    assert json.loads(case.log.read_text().splitlines()[-1]) == {"closed": True, "remaining_pins": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("forgery", ["metadata", "request_id", "execution_id"])
async def test_real_worker_native_request_uses_only_its_parent_execution_owner(tmp_path, monkeypatch, forgery):
    async with native_companion(tmp_path, monkeypatch, choice={"action": "decline"}) as case:
        owner = {"session_id": "receiving-session", "conversation_id": OWNER["session_id"], "turn_id": OWNER["turn_id"]}
        code = """
var workerModule = await import('node:worker_threads');
var originalPost = workerModule.parentPort.postMessage;
workerModule.parentPort.postMessage = function(message, ...args) {
  if (message.kind === 'native_request') {
    message = {...message, requestMeta: {'x-codex-turn-metadata': {
      session_id: 'foreign-thread', thread_id: 'foreign-thread', turn_id: 'foreign-turn'}}};
    if (FORGERY === 'request_id') message.requestId = 'foreign-request';
    if (FORGERY === 'execution_id') message.executionId += 10000;
  }
  return originalPost.call(this, message, ...args);
};
await cua.getState();
""".replace("FORGERY", json.dumps(forgery))
        result = await case.client.call_tool("js", {"code": code}, request_owner=owner)
        rows = [json.loads(line) for line in case.log.read_text().splitlines()]
        native_calls = [row for row in rows if row.get("method") == "list_windows"]
        if forgery == "metadata":
            assert not result.is_error, text(result)
            assert len(native_calls) == 1
            assert native_calls[0]["owner"] == {**OWNER, "thread_id": OWNER["session_id"]}
        else:
            assert result.is_error and "no matching active parent execution" in text(result)
            assert native_calls == []


@pytest.mark.asyncio
async def test_real_worker_cannot_reuse_a_completed_execution_for_native_work(tmp_path, monkeypatch):
    async with native_companion(tmp_path, monkeypatch, choice={"action": "decline"}) as case:
        owner = {"session_id": "receiving-session", "conversation_id": OWNER["session_id"], "turn_id": OWNER["turn_id"]}
        first = await case.client.call_tool("js", {"code": """
var workerModule = await import('node:worker_threads');
var originalPost = workerModule.parentPort.postMessage;
var heldNativeRequest;
workerModule.parentPort.postMessage = function(message, ...args) {
  if (message.kind === 'native_request') { heldNativeRequest = message; return; }
  return originalPost.call(this, message, ...args);
};
var heldOutcome = cua.getState().then(()=>'UNEXPECTED_NATIVE_EXECUTION', error=>error.message);
nodeRepl.write('original execution completed without sending its native request');
"""}, request_owner=owner)
        assert not first.is_error, text(first)
        # A different live parent execution cannot lend authority to the old
        # execution ID, even when its raw metadata names another owner.
        second = await case.client.call_tool("js", {"code": """
heldNativeRequest.requestMeta = {'x-codex-turn-metadata': {
  session_id:'foreign-thread',thread_id:'foreign-thread',turn_id:'foreign-turn'}};
originalPost.call(workerModule.parentPort, heldNativeRequest);
nodeRepl.write(await heldOutcome);
"""}, request_owner={**owner, "turn_id": "following-turn"})
        assert not second.is_error, text(second)
        assert text(second) == "Computer Use request has no matching active parent execution"
        rows = [json.loads(line) for line in case.log.read_text().splitlines()]
        assert not [row for row in rows if row.get("method") == "list_windows"]


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", [{"action": "decline"}, {"action": "accept", "content": {}}, {"action": "accept", "content": {"persist": "always"}}])
async def test_real_mcp_unapproved_app_never_reaches_native_action(tmp_path, monkeypatch, choice):
    async with native_companion(tmp_path, monkeypatch, choice=choice) as case:
        result = await case.client.call_tool("js", {"code": "var app = await cua.getApp({window_id:'11'}); await app.click({x:1,y:1});"},
            request_owner={"session_id": "receiving-session", "conversation_id": OWNER["session_id"], "turn_id": OWNER["turn_id"]})
        assert result.is_error and "not approved" in text(result)
        assert len(case.approvals) == 1
        rows = [json.loads(line) for line in case.log.read_text().splitlines()]
        assert not {"authorize", "click", "screenshot"} & {row.get("method") for row in rows}


@pytest.mark.asyncio
async def test_captured_denied_app_policy_cannot_be_replaced_by_ui_approval(tmp_path, monkeypatch):
    async with native_companion(tmp_path, monkeypatch, choice={"action": "accept", "content": {"persist": "session"}},
            app_access={"default_app_access": "deny"}) as case:
        result = await case.client.call_tool("js", {"code": "await cua.getApp({window_id:'11'})"},
            request_owner={"session_id": "receiving-session", "conversation_id": OWNER["session_id"], "turn_id": OWNER["turn_id"]})
        assert result.is_error and "captured configuration" in text(result)
        assert case.approvals == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["timeout", "owner_cancel", "reset", "turn_close", "thread_close"])
async def test_blocked_native_process_is_reclaimed_by_its_actual_owner_without_waiting_for_stdin(tmp_path, monkeypatch, caplog, ending):
    caplog.set_level("INFO", logger="backend.mcp.client")
    async with native_companion(tmp_path, monkeypatch, choice={"action": "accept", "content": {"persist": "session"}}, blocked=True) as case:
        cancel = asyncio.Event()
        active = asyncio.create_task(case.client.call_tool("js", {"code": "var app=await cua.getApp({window_id:'11'}); await app.screenshot();",
            "timeout_ms": 1500 if ending == "timeout" else 30000}, request_owner={"session_id": "receiving-session", "conversation_id": OWNER["session_id"],
                "turn_id": OWNER["turn_id"], "cancel_event": cancel}))
        async with asyncio.timeout(5):
            while True:
                rows = [json.loads(line) for line in case.log.read_text().splitlines()]
                blocked = next((row for row in rows if row.get("blocked")), None)
                if blocked:
                    break
                if active.done():
                    pytest.fail(text(await active))
                await asyncio.sleep(0.01)
        original = psutil.Process(blocked["pid"])
        original_created = original.create_time()
        assert blocked["owner"] == {**OWNER, "thread_id": OWNER["session_id"]}
        if ending == "owner_cancel":
            cancel.set()
            with pytest.raises(asyncio.CancelledError):
                await active
        elif ending == "reset":
            result = await asyncio.wait_for(case.client.call_tool("js_reset"), 5)
            assert not result.is_error, text(result)
        elif ending == "turn_close":
            assert await asyncio.wait_for(case.client.host.request("closeActiveTurn",
                {"sessionId": OWNER["session_id"], "turnId": OWNER["turn_id"]}), 5)
        elif ending == "thread_close":
            await asyncio.wait_for(case.owner.aclose(), 5)
        if ending != "owner_cancel":
            result = await asyncio.wait_for(active, 5)
            assert result.is_error
        if ending != "thread_close":
            following = await asyncio.wait_for(case.client.call_tool("js", {"code": "await cua.getState()"},
                request_owner={"session_id": "receiving-session", "conversation_id": OWNER["session_id"], "turn_id": "following-turn"}), 5)
            assert not following.is_error, text(following)
        assert not original.is_running() or original.create_time() != original_created
        rows = [json.loads(line) for line in case.log.read_text().splitlines()]
        cleanup = next(row for row in rows if row.get("bootstrap_cleanup"))
        assert cleanup["bootstrap_cleanup"][0]["owner"] == blocked["owner"]
        receipts = [record.args[2] for record in caplog.records if record.name == "backend.mcp.client"
            and isinstance(record.args, tuple) and len(record.args) == 3 and isinstance(record.args[2], dict)
            and record.args[2].get("pid") == blocked["pid"]]
        expected_reason = ("receiving thread closed" if ending == "thread_close" else
            "original turn cancelled" if ending == "turn_close" else "original kernel cancelled")
        assert receipts and receipts[0]["code"] != 0 and receipts[0]["reason"] == expected_reason


@pytest.mark.asyncio
async def test_existing_sdk_mcp_initialization_selects_the_independent_provider_and_preserves_turn_cleanup(sdk_owner, monkeypatch, request):
    from contextlib import aclosing
    from backend.agent.run_context import RunContext
    from backend.sdk import create_session, create_tool_registry
    from backend.permissions.context import PermissionContext
    from backend.tests.test_sdk_cua_ownership_chain import CuaModel, assert_tool_value
    import backend.mcp.native_cua_runtime as module

    workspace, runtime, artifacts, config = sdk_owner
    log = workspace / "native-sdk.jsonl"
    production_source = (module.ROOT / "native_cua_windows.py").read_text(encoding="utf-8")
    # Carry every production byte through the real SDK/executor command. Only
    # its OS facade changes; the original main loop and dispatch remain real.
    source = ("import sys;sys.path.insert(0," + repr(str(Path(__file__).parents[2])) + ")\n"
        "import json,os\nfrom pathlib import Path\n"
        "from backend.tests.fixtures.native_cua_receiving import FixtureDesktop\n"
        "core={'__name__':'minicode_native_protocol_fixture'}\n"
        "exec(compile(" + repr(production_source) + ",'native_cua_windows.py','exec'),core)\n"
        "log=Path(os.environ['MINICODE_CUA_FIXTURE_LOG'])\n"
        "class RecordedSession(core['NativeCuaSession']):\n"
        " def dispatch(self,method,args,owner):\n"
        "  result=super().dispatch(method,args,owner)\n"
        "  with log.open('a',encoding='utf-8') as stream: stream.write(json.dumps({'method':method,'args':args,'owner':owner})+'\\n')\n"
        "  return result\n"
        " def close(self):\n"
        "  super().close()\n"
        "  with log.open('a',encoding='utf-8') as stream: stream.write(json.dumps({'closed':True,'remaining_pins':len(self.desktop.pins)})+'\\n')\n"
        "core['WindowsDesktop']=FixtureDesktop\ncore['NativeCuaSession']=RecordedSession\ncore['main']()\n")
    build, native_config = module.build_native_bootstrap, module.native_cua_config
    payloads = []
    def full_production_payload(**kwargs):
        original = build(**kwargs)
        controlled = build(**kwargs, native_source=source)
        assert len(controlled) >= len(original)
        payloads.append({"production_bootstrap_chars": len(original), "controlled_bootstrap_chars": len(controlled)})
        return controlled
    monkeypatch.setattr(module, "build_native_bootstrap", full_production_payload)
    # Substitute only the native Windows boundary for protocol CI on other OSes.
    monkeypatch.setattr(module, "native_cua_config", lambda selected, attachment, access: native_config(selected,
        replace(attachment, peer_info={**attachment.peer_info, "platformOs": "windows"}), access))
    selected = MCPServerConfig("cua_repl", command=shutil.which("node"), env={
        "MINICODE_CUA_PROVIDER": PROVIDER, "CUA_REPL_ENABLED_SURFACES": "computer", "MINICODE_CUA_FIXTURE_LOG": str(log)})
    host = MCPServerManager(config_path=workspace / "mcp.json", workspace_root=workspace, runtime_configs=(selected,))
    run = RunContext(agent_runtime=runtime, mcp_manager=host)
    model = CuaModel("await cua.getState();nodeRepl.write('native provider initialized')", run)
    async def approve_tool(_call_id):
        return {"action": "approve"}
    session = create_session(session_id="independent-cua-sdk", workspace_root=workspace, config=config,
        llm=model, artifact_store=artifacts, tool_registry=create_tool_registry(), run_context=run,
        permission_context=PermissionContext(mode="bypass"),
        approval_handler=approve_tool,
        metadata={"conversation_id": "independent-cua-sdk"})
    try:
        async with aclosing(session.query("Use the selected MiniCode computer provider")) as stream:
            events = [event async for event in stream]
        assert_tool_value(events, "native provider initialized")
        [client] = model.clients
        assert client.native and client.runtime is None
        assert {tool.name for tool in await client.list_tools()} == {"js", "js_reset", "js_add_node_module_dir"}
        assert payloads and len(client._args[-1]) == payloads[0]["controlled_bootstrap_chars"]
        request.node.user_properties.append(("full_native_production_payload", json.dumps({
            **payloads[0], "production_source_sha256": hashlib.sha256(production_source.encode()).hexdigest(),
            "production_source_bytes": len(production_source.encode()), "node_executable": client._command,
            "receiving_argv_chars": len(subprocess.list2cmdline([client._command, *client._args])),
            "os_facade": "FixtureDesktop; no native Windows calls"})))
        rows = [json.loads(line) for line in log.read_text().splitlines()]
        observed = next(row for row in rows if row.get("method") == "list_windows")
        ended = [row for row in rows if row.get("method") == "end_turn"]
        assert observed["owner"]["session_id"] == "independent-cua-sdk"
        assert ended and ended[-1]["owner"] == {"session_id": "independent-cua-sdk", "turn_id": observed["owner"]["turn_id"]}
    finally:
        await session.aclose()
        await host.stop_all()
    assert json.loads(log.read_text().splitlines()[-1]) == {"closed": True, "remaining_pins": 0}
