"""Source network review consumes immutable command admission and real owners."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import os
from pathlib import Path
import shlex
import socket
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agent.guardian.contract import GuardianReceipt
from backend.hooks.manager import HookResult
from backend.network.approval import NetworkApprovalService
from backend.network.native import NativeNetworkRuntime
from backend.permissions.environment_scope import executor_permission_profile
from backend.permissions.grants import PermissionGrantResponse
from backend.sandbox.policy import AdditionalPermissionProfile, FileSystemPath, NetworkPermissions, SandboxPolicy
from backend.tests.test_native_exec_policy_chain import authorize, setup


class ReviewHook:
    def __init__(self, result):
        self.run_permission_request = AsyncMock(return_value=result)
        self.context = None

    def fork_for_turn(self, *, workspace_root):
        return self

    def bind_runtime(self, *, llm, tool_registry, tool_context):
        self.context = tool_context


@pytest.fixture
def network_host(tmp_path):
    host = setup(tmp_path, approval_policy="on-request")
    host.context.sandbox_policy = SandboxPolicy.workspace_default(host.workspace)
    host.service = NetworkApprovalService(conversation_id=host.context.conversation_id)
    host.service.bind_review_source(lambda: host.context)
    host.cancelled = []
    host.registrations = []
    yield host
    host.context.artifact_store.shutdown()


def register(host, *, ident="execution", context=None, persist=None, command="Write-Output 'original payload'"):
    _, authorization = authorize(host, ident=f"call-{ident}", command=command)
    context = context or host.context
    request = authorization.request
    command = replace(request.exec_policy.command, environment_id=context.environment_id)
    request = replace(request, exec_policy=replace(request.exec_policy, command=command))

    async def cancel(reason):
        # The user-facing outcome must exist before terminating this process.
        assert registration.outcome == reason
        host.cancelled.append((ident, reason))

    registration = host.service.register(request, context, execution_id=ident,
        cancel_execution=cancel, persist_amendment=persist)
    host.registrations.append(registration)
    return registration


def native_request(registration, *, host="Example.COM", protocol="http", port=8080):
    return {"executionId": registration.execution_id, "environmentId": registration.environment_id,
        "host": host, "protocol": protocol, "port": port}


@pytest.mark.asyncio
@pytest.mark.parametrize("decision,allowed,cached", [("approved", True, False),
    ("approved_for_session", True, True), ("abort", False, False)])
async def test_original_process_decision_and_source_session_scope(network_host, decision, allowed, cached):
    host = network_host
    response = {"action": "reject" if decision == "abort" else "approve", "decision": decision}
    host.context.approval_handler = AsyncMock(return_value=response)
    registration = register(host)
    result = await registration.decide(native_request(registration))
    assert (result["type"] == "allow") is allowed
    prompt = next(payload for kind, payload in host.events if kind == "approval_request")
    assert prompt["command"] == ["network-access", "http://Example.COM:8080"]
    assert prompt["available_decisions"] == ["approved", "approved_for_session", {
        "network_policy_amendment": {"network_policy_amendment": {"host": "Example.COM", "action": "allow"}}}, "abort"]
    assert prompt["tool_call_id"].startswith("network#local#http#example.com#8080#")
    assert prompt["request_digest"] != registration.request.digest
    assert prompt["network_approval_context"] == {"host": "Example.COM", "protocol": "http"}
    if allowed:
        await registration.decide(native_request(registration, host="EXAMPLE.com"))
        assert host.context.approval_handler.await_count == (1 if cached else 2)
    assert len(host.cancelled) == (0 if allowed else 1)
    assert await registration.finish() == (None if allowed else "rejected by user")
    assert await registration.finish() == registration.outcome


@pytest.mark.asyncio
async def test_pending_dedup_is_same_process_not_same_domain_only(network_host):
    host = network_host
    entered = asyncio.Queue()
    response = asyncio.get_running_loop().create_future()

    async def review(ident):
        await entered.put(ident)
        return await asyncio.shield(response)

    host.context.approval_handler = review
    first, second = register(host, ident="one"), register(host, ident="two")
    pending = [asyncio.create_task(first.decide(native_request(first))) for _ in range(3)]
    pending.append(asyncio.create_task(second.decide(native_request(second))))
    ids = {await entered.get(), await entered.get()}
    assert len(ids) == 2 and entered.empty()
    response.set_result({"action": "approve", "decision": "approved"})
    assert await asyncio.gather(*pending) == [{"type": "allow"}] * 4
    assert host.service._pending == {}
    await first.finish()
    await second.finish()


@pytest.mark.asyncio
async def test_session_approval_environment_protocol_and_port_are_independent(network_host):
    host = network_host
    host.context.approval_handler = AsyncMock(return_value={"action": "approve", "decision": "approved_for_session"})
    first = register(host)
    await first.decide(native_request(first))
    for changes in ({"port": 8081}, {"protocol": "https_connect"}):
        assert await first.decide(native_request(first, **changes)) == {"type": "allow"}
    remote = register(host, ident="other-env", context=replace(host.context, environment_id="other"))
    assert await remote.decide(native_request(remote)) == {"type": "allow"}
    assert host.context.approval_handler.await_count == 4
    mismatch = native_request(first)
    mismatch["environmentId"] = "other"
    assert (await first.decide(mismatch))["type"] == "deny"
    assert host.context.approval_handler.await_count == 4
    await first.finish()
    await remote.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize("active,policy,managed,prompts", [(False, "on-request", True, 0),
    (True, "never", True, 0), (True, "on-request", False, 0), (True, "granular", True, 1)])
async def test_real_policy_admission_does_not_make_an_autoallow_for_misses(network_host, active, policy, managed, prompts):
    host = network_host
    host.context.permission = replace(host.context.permission, approval_policy=policy,
        granular_approval={"sandbox_approval": False, "rules": False, "mcp_elicitations": False} if policy == "granular" else None)
    host.context.approval_handler = AsyncMock(return_value={"action": "approve"})
    registration = register(host)
    registration.command = replace(registration.command, managed_profile=managed)
    if not active:
        host.service.bind_review_source(lambda: None)
    result = await registration.decide(native_request(registration))
    assert (result["type"] == "allow") is bool(prompts)
    assert host.context.approval_handler.await_count == prompts
    assert await registration.finish() == (None if prompts else 'Network access to "http://Example.COM:8080" was blocked by policy.')


@pytest.mark.asyncio
@pytest.mark.parametrize("applied,action,allowed", [(True, "allow", True), (False, "allow", False), (True, "deny", False)])
async def test_source_runtime_applied_then_disk_failure_preserves_true_result(network_host, applied, action, allowed):
    host = network_host
    seen = []

    async def persist(**kwargs):
        seen.append({key: value for key, value in kwargs.items() if key != "on_policy_applied"})
        if applied:
            kwargs["on_policy_applied"]()
        raise OSError("actual controlled read-only policy volume")

    host.context.approval_handler = AsyncMock(return_value={"action": "approve" if action == "allow" else "reject",
        "decision": {"network_policy_amendment": {"network_policy_amendment": {"host": "Example.COM", "action": action}}}})
    registration = register(host, persist=persist)
    result = await registration.decide(native_request(registration, protocol="socks5_tcp"))
    assert (result["type"] == "allow") is allowed
    assert seen[0]["protocol"] == "socks5_tcp"
    assert seen[0]["approved_host"] == "Example.COM"
    assert any("actual controlled read-only policy volume" in payload["message"] for kind, payload in host.events if kind == "system_notice")
    if allowed:
        assert await registration.decide(native_request(registration, protocol="socks5_tcp")) == {"type": "allow"}
        assert host.context.approval_handler.await_count == 1
    if action == "deny":
        assert registration.outcome == "rejected by user"
        registration.record_outcome("generic disconnect")
        assert registration.outcome == "rejected by user"
    await registration.finish()


@pytest.mark.asyncio
async def test_amendment_host_equivalence_is_owned_by_native_callback_not_python(network_host):
    host = network_host
    captured = []

    async def persist(**kwargs):
        captured.append(kwargs)
        kwargs["on_policy_applied"]()

    host.context.approval_handler = AsyncMock(return_value={"action": "approve", "decision": {
        "network_policy_amendment": {"network_policy_amendment": {"host": "example.com", "action": "allow"}}}})
    registration = register(host, persist=persist)
    assert await registration.decide(native_request(registration)) == {"type": "allow"}
    assert captured[0]["host"] == "example.com" and captured[0]["approved_host"] == "Example.COM"
    await registration.finish()


@pytest.mark.asyncio
async def test_process_finished_cancels_pending_review_without_new_failure(network_host):
    host = network_host
    entered = asyncio.Event()

    async def review(_):
        entered.set()
        await asyncio.Future()

    host.context.approval_handler = review
    registration = register(host)
    pending = asyncio.create_task(registration.decide(native_request(registration)))
    await entered.wait()
    assert await registration.finish() is None
    assert (await pending)["type"] == "deny"
    assert not host.cancelled
    assert host.service._pending == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_source", ["process_finished", "request_disconnect"])
async def test_ready_review_commit_precedes_concurrent_process_finished(network_host, cancel_source):
    host = network_host
    committing, release = asyncio.Event(), asyncio.Event()

    async def persist(**kwargs):
        committing.set()
        await release.wait()
        kwargs["on_policy_applied"]()

    host.context.approval_handler = AsyncMock(return_value={"action": "approve", "decision": {
        "network_policy_amendment": {"network_policy_amendment": {"host": "Example.COM", "action": "allow"}}}})
    registration = register(host, persist=persist)
    pending = asyncio.create_task(registration.decide(native_request(registration)))
    await committing.wait()
    finished = asyncio.create_task(registration.finish()) if cancel_source == "process_finished" else None
    if cancel_source == "request_disconnect":
        pending.cancel()
    await asyncio.sleep(0)
    assert not (finished or pending).done()
    release.set()
    assert await pending == {"type": "allow"}
    assert await (finished or registration.finish()) is None


@pytest.mark.asyncio
async def test_current_reviewer_uses_original_trigger_and_source_bash_hook(network_host):
    host = network_host
    pending = asyncio.get_running_loop().create_future()
    host.context.metadata["actual_future"] = pending
    registration = register(host)
    hook = ReviewHook(HookResult())
    received = []

    async def decide(action, context, **kwargs):
        received.append((action, context, kwargs))
        return GuardianReceipt("actual-review", action.request_digest, action.policy_identity,
            action.turn_id, action.environment_id, "approved")

    active = replace(host.context, turn_id="new-active-turn", metadata={"assistant_message_id": "new-message"},
        run_context=replace(host.context.run_context, hook_manager=hook,
            guardian_review_service=SimpleNamespace(decide=decide)))
    active.permission_grants.record(PermissionGrantResponse(AdditionalPermissionProfile(network=NetworkPermissions(True)),
        "turn", True), active.environment_id, active.turn_id)
    host.service.bind_review_source(lambda: active)
    assert await registration.decide(native_request(registration, protocol="socks5_udp")) == {"type": "allow"}
    action, context, options = received[0]
    assert context.turn_id == action.turn_id == "new-active-turn"
    assert action.payload["trigger"]["callId"] == registration.request.tool_call_id
    assert action.payload["trigger"]["command"] == list(registration.command.argv)
    assert action.payload["trigger"]["cwd"] == registration.cwd
    assert action.payload["protocol"] == "socks5_udp"
    assert action.environment_id == "local" and "environment_id" not in action.payload
    assert context.metadata["assistant_message_id"] == "new-message"
    assert options["strict_auto_review"] is True
    assert "additionalPermissions" not in action.payload["trigger"]
    assert registration.context.metadata["actual_future"] is pending
    assert options["retry_reason"] == 'Network access to "socks5-udp://Example.COM:8080" was blocked by policy.'
    args, kwargs = hook.run_permission_request.await_args
    assert args == ("Bash", {"command": "Write-Output 'original payload'", "description": "network-access socks5-udp://Example.COM:8080"})
    assert kwargs["tool_call_id"].endswith("#execution")
    assert hook.context is context
    await registration.finish()


@pytest.mark.asyncio
async def test_hook_denial_precedes_guardian_and_user(network_host):
    host = network_host
    host.context.run_context.hook_manager = ReviewHook(HookResult(permission_decision="deny", message="Actual configured network veto"))
    guardian = host.context.run_context.guardian_review_service = SimpleNamespace(decide=AsyncMock())
    host.context.approval_handler = AsyncMock()
    registration = register(host)
    assert (await registration.decide(native_request(registration)))["type"] == "deny"
    assert registration.outcome == "Actual configured network veto"
    guardian.decide.assert_not_awaited()
    host.context.approval_handler.assert_not_awaited()
    await registration.finish()


@pytest.mark.asyncio
async def test_native_block_observer_retains_original_explicit_denial_and_never_cancels_turn(network_host):
    host = network_host
    turn_cancel = host.context.cancel_event = asyncio.Event()
    registration = register(host)
    registration.record_outcome("Automatic approval review rejected this original action", explicit=True)
    await registration.observe_blocked('Network access to "blocked.invalid" was blocked by policy.')
    assert host.cancelled == [("execution", "Automatic approval review rejected this original action")]
    assert not turn_cancel.is_set()
    assert await registration.finish() == "Automatic approval review rejected this original action"
    await registration.observe_blocked("late native audit cannot change finished outcome")
    assert registration.outcome == "Automatic approval review rejected this original action"


def native_policy(host, **controller):
    profile = host.context.sandbox_policy.permission_profile
    profile = replace(profile, file_system=replace(profile.file_system, entries=tuple(
        replace(entry, path=FileSystemPath("path", Path(entry.path.value).as_uri()))
        if entry.path.kind == "path" else entry for entry in profile.file_system.entries)))
    with socket.socket() as http_listener, socket.socket() as socks_listener:
        http_listener.bind(("127.0.0.1", 0))
        socks_listener.bind(("127.0.0.1", 0))
        http_port, socks_port = http_listener.getsockname()[1], socks_listener.getsockname()[1]
    return {"controller": {"enabled": True, "proxy_url": f"http://127.0.0.1:{http_port}", "socks_url": f"socks5://127.0.0.1:{socks_port}",
            "allow_upstream_proxy": False, "allow_local_binding": True, "domains": {}, **controller},
        "requirements": None, "environmentPolicy": None,
        "permissionProfile": executor_permission_profile(profile),
        "platformOs": "windows" if sys.platform == "win32" else sys.platform, "windowsSandbox": "disabled", "execPolicySources": []}


def native_runtime():
    # The test runner explicitly selects the staged build before packaging.
    import os
    directory = os.environ.get("MINICODE_NETWORK_RUNTIME")
    return NativeNetworkRuntime(directory=Path(directory) if directory else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approved", "abort"])
async def test_actual_native_proxy_pauses_original_post_and_never_replays_body(network_host, decision):
    from backend.network.policy import NetworkExecutionPolicy
    from backend.workspace.executor_local import LocalProcessBackend

    listener_config = {}
    if sys.platform == "win32":
        if not os.environ.get("MINICODE_NETWORK_WINDOWS_PROVISIONED"):
            pytest.skip("Requires explicitly provisioned owned Windows proxy listener ports; automatic system setup is excluded")
        listener_config = {"proxy_url": f"http://127.0.0.1:{int(os.environ['MINICODE_TEST_NETWORK_HTTP_PORT'])}",
            "socks_url": f"socks5://127.0.0.1:{int(os.environ['MINICODE_TEST_NETWORK_SOCKS_PORT'])}"}

    host = network_host
    entered, respond, audited = asyncio.Event(), asyncio.Event(), asyncio.Event()
    received, native_requests, cancelled_processes = [], [], []
    origin_tasks = set()

    async def origin(reader, writer):
        task = asyncio.current_task()
        origin_tasks.add(task)
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            length = int(next(line.split(b":", 1)[1] for line in header.split(b"\r\n") if line.lower().startswith(b"content-length:")))
            received.append((header, await reader.readexactly(length)))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 11\r\nConnection: close\r\n\r\nORIGINAL_OK")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            origin_tasks.discard(task)

    async def review(_):
        entered.set()
        await respond.wait()
        return {"action": "approve" if decision == "approved" else "reject", "decision": decision}

    async def native_decide(request):
        native_requests.append(dict(request))
        return await registration.decide(request)

    host.context.approval_handler = review
    runtime = native_runtime()
    server = await asyncio.start_server(origin, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    started_path, finished_path = host.workspace / "original-post-started.txt", host.workspace / "original-post-finished.txt"
    body = "original one-shot payload 中文".encode()
    request = f"POST http://127.0.0.1:{port}/one-shot HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body
    attribution_frame = ("token=os.environ['CODEX_NETWORK_PROXY_ATTRIBUTION'].encode()\n"
        "connection.sendall(b'\\x00CDXPXY1'+len(token).to_bytes(2,'big')+token)\n"
        if sys.platform != "win32" else "")
    code = ("import os,socket\nfrom urllib.parse import urlsplit\nfrom pathlib import Path\n"
        f"with Path({str(started_path)!r}).open('a',encoding='utf-8') as marker: marker.write(str(os.getpid())+'\\n')\n"
        "proxy=urlsplit(os.environ['HTTP_PROXY'])\n"
        "connection=socket.create_connection((proxy.hostname,proxy.port),5)\n"
        f"{attribution_frame}"
        f"connection.sendall({request!r})\n"
        "response=bytearray()\n"
        "while True:\n chunk=connection.recv(65536)\n if not chunk: break\n response.extend(chunk)\n"
        "connection.close()\nprint(bytes(response).decode(),flush=True)\n"
        f"Path({str(finished_path)!r}).write_text(str(os.getpid()),encoding='utf-8')\n")
    argv = [sys.executable, "-I", "-S", "-u", "-c", code]
    command = ("& '" + sys.executable.replace("'", "''") + "' -I -S -u -c '" + code.replace("'", "''") + "'"
        if sys.platform == "win32" else shlex.join(argv))
    registration = register(host, command=command)
    backend = LocalProcessBackend()
    borrowers, processes = [], []

    def observe(process):
        processes.append(process)
        if sys.platform == "win32":
            borrowers.append(process.network_lease.clone())

    original_cancel = registration.cancel_execution

    async def cancel(reason):
        await original_cancel(reason)
        cancelled_processes.append(processes[0])
        if not processes[0].exited:
            await processes[0].terminate()

    registration.cancel_execution = cancel
    try:
        captured = native_policy(host, **listener_config)
        environment = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "PATH") if key in os.environ}
        if sys.platform == "win32":
            captured["windowsSandbox"] = "elevated"
            inspected = await runtime.inspect(captured)
            policy = NetworkExecutionPolicy(registration.environment_id, runtime, captured,
                inspected["effectivePolicy"], inspected["remoteLaunch"])
            sandbox = {"permissions": captured["permissionProfile"],
                "policyContext": {"cwd": host.workspace.as_uri(), "workspaceRoots": [host.workspace.as_uri()]},
                "windowsSandboxLevel": "elevated"}
            network_launch = {"managed_network_policy": policy, "network_decider": native_decide,
                "network_observer": lambda _event: audited.set()}
        else:
            # Exercise Source's trusted bridge protocol with an owned client;
            # this does not claim Linux local OS managed-network routing.
            attribution_token = f"original-post-{decision}"
            lease = await runtime.prepare_local(captured, environment,
                environment_id=registration.environment_id, execution_id=registration.execution_id,
                attribution_token=attribution_token, decider=native_decide, observer=lambda _event: audited.set())
            borrowers.append(lease)
            assert lease.env["CODEX_NETWORK_PROXY_ATTRIBUTION"] == attribution_token
            environment = lease.env
            sandbox, network_launch = None, {}
        async with asyncio.timeout(20):
            process = await backend.start(list(registration.command.argv), host.workspace.as_uri(),
                sandbox=sandbox, **network_launch, process_observer=observe, process_id=registration.execution_id,
                env=environment, env_policy={"inherit": "none"})
        if sys.platform != "win32":
            assert process.network_lease is None and process.sandbox_type == "none"
        async with asyncio.timeout(10):
            await entered.wait()
            assert received == [], "The original POST must remain paused before approval"
            respond.set()
        chunks, after = [], 0
        async with asyncio.timeout(15):
            while True:
                result = await process.read(after_seq=after, wait_ms=1000)
                chunks.extend(chunk.chunk for chunk in result.chunks)
                after = result.next_seq - 1
                if result.closed:
                    break
            await audited.wait()
        response = b"".join(chunks)
        if decision == "approved":
            assert b"HTTP/1.1 200" in response and b"ORIGINAL_OK" in response, response
            assert len(received) == 1 and received[0][1] == body
            assert not host.cancelled and not cancelled_processes
            assert finished_path.read_text(encoding="utf-8") == started_path.read_text(encoding="utf-8").strip()
        else:
            assert received == [] and registration.outcome == "rejected by user"
            assert host.cancelled == [(registration.execution_id, "rejected by user")]
            assert cancelled_processes == [process] and not finished_path.exists()
        assert processes == [process]
        assert len(started_path.read_text(encoding="utf-8").splitlines()) == 1
        assert len(native_requests) == 1
        assert native_requests[0]["executionId"] == registration.execution_id
        assert native_requests[0]["environmentId"] == registration.environment_id
        assert native_requests[0]["method"] == "POST" and native_requests[0]["port"] == port
        prompts = [payload for kind, payload in host.events if kind == "approval_request"]
        assert len(prompts) == 1
        assert prompts[0]["args"]["port"] == port
        lease = borrowers[0]
        await lease.flush_audit()
        assert lease.network_audit and all(event["executionId"] == registration.execution_id
            and event["environmentId"] == registration.environment_id for event in lease.network_audit)
    finally:
        respond.set()
        try:
            server.close()
            await server.wait_closed()
            await asyncio.gather(*origin_tasks, return_exceptions=True)
        finally:
            try:
                await registration.finish()
            finally:
                try:
                    await backend.close()
                finally:
                    for lease in borrowers:
                        await lease.aclose()
                    await runtime.aclose()
    assert all(lease.closed for lease in borrowers)
    assert registration.execution_id not in runtime.leases


@pytest.mark.asyncio
async def test_native_remote_constraints_precede_consumer_and_persisted_source_rules_are_reusable(network_host):
    host = network_host
    host.context.approval_handler = AsyncMock(return_value={"action": "approve", "decision": "approved"})
    registration = register(host)
    runtime = native_runtime()
    lease = None
    try:
        lease = await runtime.prepare_remote(native_policy(host, domains={"hard-deny.invalid": "deny"}),
            environment_id=registration.environment_id, execution_id=registration.execution_id,
            attribution_token="original-remote", decider=registration.decide)
        denied = await lease.decide(native_request(registration, host="hard-deny.invalid"))
        assert denied["type"] == "deny"
        host.context.approval_handler.assert_not_awaited()
        assert await lease.decide(native_request(registration, host="ask-owned.invalid")) == {"type": "allow"}
        assert host.context.approval_handler.await_count == 1
        await lease.apply_amendment("permanent-owned.invalid", "http", "allow")
        assert await lease.decide(native_request(registration, host="permanent-owned.invalid")) == {"type": "allow"}
        assert host.context.approval_handler.await_count == 1
        await host.owner.persist_network_amendment(runtime, host="permanent-owned.invalid", protocol="http", action="allow")
        contents = host.owner.default_policy_path.read_text(encoding="utf-8")
        assert 'network_rule(host="permanent-owned.invalid",protocol="http",decision="allow"' in contents.replace(" ", "")
        result = await runtime.inspect({**native_policy(host), "execPolicySources": [{"path": str(host.owner.default_policy_path), "contents": contents}]})
        assert result["enabled"]
        assert result["effectivePolicy"]["domains"]["permanent-owned.invalid"] == "allow"
    finally:
        await registration.finish()
        if lease is not None:
            await lease.aclose()
        await runtime.aclose()


@pytest.mark.asyncio
async def test_actual_query_context_registers_live_metadata_and_retains_original_snapshot_until_finish(tmp_path):
    from backend.agent.context import ContextBuilder
    from backend.agent.environment_selection import TurnEnvironmentRequest
    from backend.agent.loop_session import AgentLoopSessionContext
    from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
    from backend.agent.run_context import RunContext
    from backend.agent.runtime import AgentRuntime
    from backend.agent.state import AgentState
    from backend.config import AgentSettings, PermissionSettings, TokenBudget
    from backend.llm.base import ToolCallEvent
    from backend.environments import EnvironmentManager
    from backend.permissions.checker import PermissionChecker
    from backend.permissions.context import PermissionContext
    from backend.tests.test_native_exec_policy_chain import output_command
    from backend.tests.test_query_native_command_start_admission_chain import NativeCommandModel
    from backend.tests.test_environments import configure
    from backend.tools.command_tool import RunCommandTool
    from backend.workspace.executor_local import LocalExecutionEnvironmentBackend

    host = setup(tmp_path, approval_policy="never")
    captured = []

    class CapturedCommand(RunCommandTool):
        async def execute(self, arguments, context):
            service = context.run_context.network_approval_service
            assert context.final_tool_request is not None
            assert context.metadata["_context_builder"] is builder
            assert context.metadata["_agent_state"] is state
            assert context.metadata["_tool_execution_context"] is builder._tool_execution_context
            registration = service.register(context.final_tool_request, context,
                execution_id="actual-query-registration", cancel_execution=AsyncMock())
            assert registration.context.metadata["_context_builder"] is builder
            assert registration.context.metadata["_tool_execution_context"] is builder._tool_execution_context
            assert registration.snapshot is not context.run_context.turn_environments
            captured.append(registration)
            return await super().execute(arguments, context)

    host.registry.unregister("run_command")
    host.registry.register(CapturedCommand(host.context.artifact_store))
    call = ToolCallEvent(id="actual-network-admission-command", name="run_command", arguments={
        "command": output_command("ACTUAL_NETWORK_REGISTRATION_METADATA")[0], "login": False, "yield_time_ms": 10000})
    settings = AgentSettings(max_iterations=3, max_turn_seconds=30, stream_max_attempts=0, code_mode_only=False)
    budget = TokenBudget(total=96000)
    model = NativeCommandModel(host.snapshot.config.llm, call)
    admitted = replace(host.snapshot, config=replace(host.snapshot.config, agent=settings, token_budget=budget), llm=model)
    runtime = AgentRuntime(metrics_file=tmp_path / "runtime/metrics.jsonl", swarm_store_dir=tmp_path / "runtime/swarm", enable_lease_heartbeat=False)
    config_dir = tmp_path / "actual-executors"
    config_dir.mkdir()
    configure(config_dir, [], default="local", include_local=True)
    manager = EnvironmentManager(config_dir, local_backend=LocalExecutionEnvironmentBackend())
    run = RunContext(agent_runtime=runtime, model_execution=admitted, exec_policy_owner=host.owner,
        environment_manager=manager, environment_requests=(TurnEnvironmentRequest("local", host.workspace.as_uri(), (host.workspace.as_uri(),)),))
    builder = ContextBuilder(llm=model, token_budget=budget, conversation_id="actual-network-metadata", workspace_root=host.workspace)
    session = AgentSession(llm=model, tool_registry=host.registry, artifact_store=host.context.artifact_store,
        permission_checker=PermissionChecker(PermissionSettings(require_confirm=[], require_diff_review=[]), host.workspace),
        agent_settings=settings, token_budget=budget, context_builder=builder)
    state = AgentState(user_message="Run the controlled command and retain its actual network admission snapshot.",
        conversation_id="actual-network-metadata", workspace_root=host.workspace)
    try:
        events = [event async for event in QueryEngine().submit(QuerySubmission(user_message=state.user_message, session=session, state=state,
            runtime=AgentLoopSessionContext(session_id="actual-network-metadata-session", workspace_root=host.workspace,
                run_context=run, permission_context=PermissionContext(mode="bypass", approval_policy="never",
                    sandbox_mode="danger-full-access", workspace_root=host.workspace), cancel_event=asyncio.Event())))]
        assert state.terminal_status == "completed", [(event.type, event.data) for event in events if event.type in {"error", "done", "tool_result"}]
        assert len(captured) == 1 and model.calls == 2, [event.data.get("summary", event.data.get("message")) for event in events if event.type in {"error", "tool_result"}]
        assert any("ACTUAL_NETWORK_REGISTRATION_METADATA" in event.data.get("summary", "") for event in events if event.type == "tool_result")
        registration = captured[0]
        assert registration.snapshot.turn_environments()
        assert registration.snapshot._resources.close_task is None
        assert not session.active_turn
        assert session.network_approval_service._review_source() is None
        assert await registration.finish() is None
        assert registration.snapshot._resources.close_task.done()
    finally:
        for registration in captured:
            await registration.finish()
        await session.aclose()
        host.context.artifact_store.shutdown()
        runtime.close(release_lease=True)
        await manager.close()
