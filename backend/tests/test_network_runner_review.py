"""Narrow ownership regressions from the independent network lifecycle audit.

These tests exercise the actual executor handle and runner lifecycle with an
explicitly failed peer boundary. They do not claim native proxy enforcement.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.environments.errors import ExecutorDisconnected, ExecutorProtocolError, ExecutorRpcError
from backend.environments.process import ExecutorProcess
from backend.environments.rpc import RpcConnection
from backend.network.native import NativeNetworkRuntime
from backend.tests.test_network_approval import network_host, native_request, register
from backend.terminal.executor_runner import ExecutorCommandRunner


def owner_fixture(*, read_failure=False, observer_failure=False, peer_available=False):
    peer = SimpleNamespace(available=not read_failure or peer_available)
    async def request(method, params, *, cleanup=False):
        assert params["processId"] == "original-process"
        assert cleanup
        if not peer.available:
            raise ExecutorDisconnected("original peer connection is unavailable")
        if method == "process/terminate":
            return {"running": True}
        assert method == "process/read"
        return {"chunks": [], "nextSeq": 3, "exited": True, "exitCode": 137, "closed": True, "failure": None}
    client = SimpleNamespace(processes={}, closed=False,
        notify_idle=Mock(), request=AsyncMock(side_effect=request))
    handle = ExecutorProcess(client, "original-process", None)
    client.processes[handle.process_id] = handle
    registration = SimpleNamespace(decide=AsyncMock(), finish=AsyncMock(return_value=None), observe_blocked=AsyncMock())
    lease = SimpleNamespace(remote_launch={}, decide=AsyncMock(), network_audit=[], flush_audit=AsyncMock(), aclose=AsyncMock())
    connection = SimpleNamespace(request=AsyncMock(side_effect=ExecutorRpcError(-32000, "actual deniedMessage RPC failed")))
    network = SimpleNamespace(prepare_remote=AsyncMock(return_value=lease), runtime=SimpleNamespace(connection=AsyncMock(return_value=connection)))

    reader_error = ExecutorProtocolError("original executor reader failed")
    async def start(*_args, process_observer, network_observer, **_kwargs):
        process_observer(handle)
        handle.accepted = True
        if read_failure:
            handle.fail(reader_error)
        else:
            if observer_failure:
                network_observer({"decision": "deny"})
            handle.publish("process/exited", {"processId": handle.process_id, "seq": 1, "exitCode": 0})
            handle.publish("process/closed", {"processId": handle.process_id, "seq": 2})
        return handle

    environment = SimpleNamespace(process=SimpleNamespace(start=start))
    attachment_lease = SimpleNamespace(environment=environment, aclose=AsyncMock())
    attachment_lease.clone = Mock(return_value=attachment_lease)
    policy = SimpleNamespace(attachment=SimpleNamespace(lease=attachment_lease, is_remote=True), timeout=None,
        network_policy=network, network_approval_factory=Mock(return_value=registration),
        scope=SimpleNamespace(environment_id="original-environment", cwd_uri="file:///original", process_sandbox_context=Mock(return_value={})),
        env_overrides={}, env_policy_wire=Mock(return_value={}))
    return SimpleNamespace(runner=ExecutorCommandRunner(policy), handle=handle, client=client, peer=peer, reader_error=reader_error, registration=registration,
        network_lease=lease, attachment_lease=attachment_lease)


@pytest.mark.asyncio
async def test_failed_reader_with_unconfirmed_close_preserves_recovery_receipt():
    owner = owner_fixture(read_failure=True)
    with pytest.raises(ExecutorProtocolError, match="original executor reader failed") as caught:
        await owner.runner.run("original", cwd="file:///original", argv=["original"])
    assert not owner.handle.closed
    # The foreground tool and BackgroundCommandManager use this field to keep
    # durable recovery ownership instead of deleting the failed command.
    assert getattr(caught.value, "cleanup_pending", False)
    assert caught.value.cleanup_reason == "executor_process_close_unconfirmed"
    assert not owner.handle.released
    assert owner.client.processes[owner.handle.process_id] is owner.handle
    assert owner.runner.handle is owner.handle
    owner.registration.finish.assert_not_awaited()
    owner.network_lease.aclose.assert_not_awaited()
    owner.attachment_lease.aclose.assert_not_awaited()
    # A later cleanup uses this retained handle and observer, rather than
    # registering a second process or borrowing a newer execution owner.
    owner.peer.available = True
    assert await owner.runner.cleanup() is True
    assert owner.runner.handle is owner.handle
    assert owner.handle.closed and owner.handle.released
    assert owner.handle.failure is owner.reader_error
    owner.registration.finish.assert_awaited_once()
    owner.network_lease.aclose.assert_awaited_once()
    owner.attachment_lease.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_original_peer_close_receipt_releases_resources_without_repairing_failed_output():
    owner = owner_fixture(read_failure=True, peer_available=True)
    with pytest.raises(ExecutorProtocolError, match="original executor reader failed") as caught:
        await owner.runner.run("original", cwd="file:///original", argv=["original"])
    assert owner.handle.closed and owner.handle.released
    assert owner.handle.failure is owner.reader_error
    assert owner.handle.last_seq == 0  # No output or close event was fabricated.
    assert owner.handle.process_id not in owner.client.processes
    assert not getattr(caught.value, "cleanup_pending", False)
    assert [call.args[0] for call in owner.client.request.await_args_list] == ["process/terminate", "process/read"]
    owner.registration.finish.assert_awaited_once()
    owner.network_lease.aclose.assert_awaited_once()
    owner.attachment_lease.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_observer_error_does_not_prevent_original_registration_and_lease_release():
    owner = owner_fixture(observer_failure=True)
    with pytest.raises(ExecutorRpcError, match="actual deniedMessage RPC failed") as caught:
        await owner.runner.run("original", cwd="file:///original", argv=["original"])
    assert owner.handle.closed
    owner.registration.finish.assert_awaited_once()
    owner.network_lease.aclose.assert_awaited_once()
    owner.attachment_lease.aclose.assert_awaited_once()
    assert owner.runner.closed
    assert not getattr(caught.value, "cleanup_pending", False)
    assert await owner.runner.cleanup() is True
    owner.network_lease.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_result_retains_audit_emitted_by_the_original_proxy_release():
    owner = owner_fixture()
    final_audit = {"decision": "deny", "reason": "not_allowed", "host": "original.example"}
    async def close_original_proxy():
        # Native Execution.close shuts down its proxy and flushes audit tasks
        # before replying to network/release. Such final records arrive here.
        owner.network_lease.network_audit.append(final_audit)
    owner.network_lease.aclose.side_effect = close_original_proxy
    result = await owner.runner.run("original", cwd="file:///original", argv=["original"])
    assert {"source": "controller", "payload": final_audit} in result.network_audit


@pytest.mark.asyncio
async def test_network_consumer_error_answers_the_original_native_rpc_waiter(network_host):
    host = network_host
    host.context.approval_handler = AsyncMock(side_effect=ExecutorProtocolError("original approval transport failed"))
    registration = await register(host)
    native = NativeNetworkRuntime()
    native.deciders[registration.execution_id] = registration.decide
    # Exercise the production inbound handler without starting a native peer;
    # only the outgoing wire sink is replaced for this boundary assertion.
    rpc = RpcConnection.__new__(RpcConnection)
    rpc.closed, rpc.failure = False, None
    rpc.inbound_request = native._inbound
    rpc.send = AsyncMock()
    try:
        await rpc._handle_request({"id": "original-native-request", "method": "network/policyRequest",
            "params": native_request(registration)})
        response = rpc.send.call_args.args[0]
        assert response["id"] == "original-native-request"
        assert "original approval transport failed" in response["error"]["message"]
        assert "original approval transport failed" in registration.outcome
        assert host.cancelled == [("execution", registration.outcome)]
    finally:
        await registration.finish()
