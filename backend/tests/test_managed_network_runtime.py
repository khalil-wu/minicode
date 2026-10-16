"""Actual pinned Source proxy policy, borrower lifetime and local WFP routing."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import os
from pathlib import Path
import socket
import subprocess
import json
import sys

import pytest
import psutil

from backend.environments.errors import ExecutorClosed, ExecutorRpcError
from backend.network.native import NativeNetworkRuntime
from backend.network.policy import NetworkExecutionPolicy
from backend.workspace.executor_local import LocalProcessBackend
from backend.sandbox.runner import SandboxUnavailableError
from backend.sandbox.windows_native import discover_runtime


def runtime():
    directory = os.environ.get("MINICODE_NETWORK_RUNTIME")
    return NativeNetworkRuntime(directory=Path(directory) if directory else None)


def source_policy():
    with socket.socket() as http, socket.socket() as socks:
        http.bind(("127.0.0.1", 0))
        socks.bind(("127.0.0.1", 0))
        http_port, socks_port = http.getsockname()[1], socks.getsockname()[1]
    http_port = int(os.environ.get("MINICODE_TEST_NETWORK_HTTP_PORT", http_port))
    socks_port = int(os.environ.get("MINICODE_TEST_NETWORK_SOCKS_PORT", socks_port))
    return {"controller": {"enabled": True, "proxy_url": f"http://127.0.0.1:{http_port}",
        "socks_url": f"socks5://127.0.0.1:{socks_port}", "domains": {}, "allow_local_binding": False,
        "allow_upstream_proxy": False},
        "requirements": None, "environmentPolicy": None,
        "permissionProfile": {"type": "managed", "file_system": {"type": "restricted", "entries": []},
            "network": "restricted"}, "platformOs": "windows" if sys.platform == "win32" else sys.platform,
        "windowsSandbox": "elevated", "execPolicySources": []}


def request(host):
    return {"host": host, "port": 443, "protocol": "https_connect"}


@pytest.mark.asyncio
async def test_actual_native_policy_ceilings_and_original_borrower_survive_new_policy():
    native = runtime()
    decisions = []

    async def approve(value):
        decisions.append(value)
        return {"type": "allow"}

    baseline = source_policy()
    baseline["controller"]["allow_local_binding"] = True
    baseline["controller"]["domains"] = {"denied.invalid": "deny", "allowed.invalid": "allow"}
    original = newer = None
    try:
        original = await native.prepare_remote(baseline, environment_id="original-environment",
            execution_id="original-execution", attribution_token="original-token", decider=approve)
        borrower = original.clone()
        await original.aclose()
        with pytest.raises(ExecutorClosed):
            original.clone()
        assert (await borrower.decide(request("DENIED.invalid.")))["type"] == "deny"
        assert not decisions
        assert await borrower.decide(request("allowed.invalid")) == {"type": "allow"}
        assert not decisions
        assert await borrower.decide(request("review.invalid")) == {"type": "allow"}
        assert decisions[0]["executionId"] == "original-execution"
        assert decisions[0]["environmentId"] == "original-environment"
        fixed = deepcopy(baseline)
        fixed["requirements"] = {"enabled": True, "managed_allowed_domains_only": True,
            "domains": {"allowed.invalid": "allow"}}
        newer = await native.prepare_remote(fixed, environment_id="new-environment", execution_id="new-execution",
            attribution_token="new-token", decider=approve)
        assert (await newer.decide(request("review.invalid")))["type"] == "deny"
        await borrower.apply_amendment("extended.invalid", "https", "allow")
        assert await borrower.decide(request("extended.invalid")) == {"type": "allow"}
        assert len(decisions) == 1
        assert "original-execution" in native.leases
        await borrower.aclose()
        assert "original-execution" not in native.leases
    finally:
        if original is not None:
            await original.aclose()
        if newer is not None:
            await newer.aclose()
        await native.aclose()


@pytest.mark.asyncio
async def test_actual_source_owner_denial_socket_ceiling_and_explicit_false_survive():
    native = runtime()
    policy = source_policy()
    policy["controller"].update(domains={"*.blocked.invalid": "deny"}, unix_sockets={},
        dangerously_allow_all_unix_sockets=False)
    policy["environmentPolicy"] = {"requiresProxy": True, "domains": {"api.blocked.invalid": "allow"},
        "unixSockets": {"C:/must-not-open.sock": "allow"}, "allowLocalBinding": True,
        "allowUpstreamProxy": True, "dangerouslyAllowAllUnixSockets": True, "managedAllowedDomainsOnly": False}
    try:
        inspected = await native.inspect(policy)
        effective = inspected["effectivePolicy"]
        assert effective["domains"]["*.blocked.invalid"] == "deny"
        assert effective["allowLocalBinding"] is False
        assert not effective["unixSockets"]
        remote = inspected["remoteLaunch"]
        assert remote["proxy"]["allowLocalBinding"] is False
        assert "127.0.0.1:0" not in str(remote)
        assert native.invoke_sync("network/normalizeHost", {"host": " EXAMPLE.com.:443 "}) == {"host": "example.com"}
        disabled = deepcopy(policy)
        disabled["controller"]["enabled"] = False
        with pytest.raises(ExecutorRpcError, match="disabled controller"):
            await native.inspect(disabled)
        cross_os = source_policy()
        cross_os["platformOs"] = "linux"
        cross_os["permissionProfile"]["file_system"]["entries"] = [
            {"path": {"type": "path", "path": "file:///remote%20project/source"}, "access": "write"}]
        before = deepcopy(cross_os)
        assert (await native.inspect(cross_os))["enabled"]
        assert cross_os == before
    finally:
        await native.aclose()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native readiness and existing-setup mode")
def test_actual_network_ready_and_require_existing_never_provision_empty_owned_home(tmp_path):
    selected, reason = discover_runtime()
    assert selected is not None, reason
    home = tmp_path / "uninitialized-owned-home"
    home.mkdir()
    settings = {"proxy_ports": [31781, 31782], "allow_local_binding": False}
    command = [str(selected.executable), "sandbox", "network-ready", "--codex-home", str(home),
        "--settings-json", json.dumps(settings)]
    ready = subprocess.run(command, capture_output=True, check=True, timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW)
    assert json.loads(ready.stdout) == {"ready": False, "settings": settings}
    assert not list(home.iterdir()), "The network-ready operation must remain read-only"
    marker = tmp_path / "must-not-start-workload"
    profile = {"type": "managed", "file_system": {"type": "restricted", "entries": [
        {"path": {"type": "special", "value": {"kind": "root"}}, "access": "read"}]}, "network": "restricted"}
    environment = {"SYSTEMROOT": os.environ["SYSTEMROOT"], "WINDIR": os.environ["WINDIR"],
        "CODEX_WINDOWS_SANDBOX_PROXY_PORTS": "31781,31782"}
    denied = subprocess.run([str(selected.executable), "--run-as-windows-sandbox", "--codex-home", str(home),
        "--command-cwd", str(tmp_path), "--permission-profile", json.dumps(profile),
        "--env-json", json.dumps(environment), "--windows-sandbox-level", "elevated",
        "--windows-sandbox-private-desktop-name", "MiniCodeSandboxDesktop-readonly-test",
        "--proxy-enforced", "--require-existing-sandbox", "--", sys.executable, "-c",
        f"from pathlib import Path; Path({str(marker)!r}).touch()"], capture_output=True, timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW)
    assert denied.returncode != 0
    assert b"explicit MiniCode sandbox setup action" in denied.stderr, denied.stderr
    assert not marker.exists()
    assert not (home / ".sandbox-secrets" / "sandbox_users.json").exists()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32", reason="Windows managed proxy prerequisite producer")
async def test_actual_local_pending_network_prerequisite_is_published_before_any_workload(tmp_path, record_testsuite_property):
    native = runtime()
    backend = LocalProcessBackend()
    observed = []
    captured = source_policy()
    inspected = await native.inspect(captured)
    policy = NetworkExecutionPolicy("owned-local", native, captured, inspected["effectivePolicy"], inspected["remoteLaunch"])
    sandbox = {"permissions": {"type": "managed", "file_system": {"type": "restricted", "entries": [
        {"path": {"type": "special", "value": {"kind": "root"}}, "access": "read"}]}, "network": "restricted"},
        "policyContext": {"cwd": tmp_path.as_uri(), "workspaceRoots": [tmp_path.as_uri()]}, "windowsSandboxLevel": "elevated"}
    marker = tmp_path / "must-not-start"
    try:
        with pytest.raises(SandboxUnavailableError, match="explicit MiniCode sandbox setup action"):
            await backend.start([sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
                tmp_path.as_uri(), sandbox=sandbox, managed_network_policy=policy, process_observer=observed.append,
                env_policy={"inherit": "none"})
        assert not marker.exists() and not observed and not backend.processes and not native.leases
        status = native.local_prerequisite.status()
        selected, reason = discover_runtime()
        assert selected is not None, reason
        assert status["required"] and not status["ready"]
        assert status["sandbox_home"] == str(selected.home)
        assert status["sandbox_executable"] == str(selected.executable)
        assert len(status["settings"]["proxy_ports"]) == 2
        assert status["settings"]["allow_local_binding"] is False
        record_testsuite_property("network_provisioning", json.dumps(status))
    finally:
        await backend.close()
        await native.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("executor_kind", ["local", "source"])
@pytest.mark.parametrize("mode", ["allow", "deny", "direct"])
async def test_actual_windows_proxy_and_wfp_bypass(mode, executor_kind, tmp_path):
    if sys.platform != "win32":
        pytest.skip("This matrix exercises Windows SID ingress and WFP")
    if not os.environ.get("MINICODE_NETWORK_WINDOWS_PROVISIONED"):
        pytest.skip("Requires explicitly provisioned owned Windows proxy listener ports; automatic system setup is excluded")
    if executor_kind == "source" and not os.environ.get("MINICODE_SOURCE_EXECUTOR"):
        pytest.skip("Requires explicitly selected owned Source exec-server")
    if executor_kind == "source" and not os.environ.get("MINICODE_SOURCE_NETWORK_HOME"):
        pytest.skip("Requires an explicitly provisioned disposable Source executor home")
    native = runtime()
    backend = LocalProcessBackend()
    manager = None
    environment_lease = None
    if executor_kind == "source":
        from backend.environments import EnvironmentManager
        from backend.tests.test_environments import configure

        configuration = tmp_path / "configuration"
        configuration.mkdir()
        home = Path(os.environ["MINICODE_SOURCE_NETWORK_HOME"]).resolve()
        assert home.is_dir()
        configure(configuration, [{"id": "source", "program": os.environ["MINICODE_SOURCE_EXECUTOR"],
            "args": ["exec-server", "--listen", "stdio", "--concurrent-requests", "4"],
            "cwd": str(tmp_path), "env": {"CODEX_HOME": str(home)}}], default="source")
        manager = EnvironmentManager(configuration, environ={})
        environment_lease = manager.borrow("source")
        selected = environment_lease.environment
        assert (await selected.info())["capabilities"]["networkProxyLaunch"] is True
        backend = selected.process
    received = []
    clients = set()

    async def origin(reader, writer):
        task = asyncio.current_task()
        clients.add(task)
        try:
            received.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 8\r\nConnection: close\r\n\r\nOWNED_OK")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            clients.remove(task)

    server = await asyncio.start_server(origin, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    audit, borrowers = [], []
    try:
        captured = source_policy()
        captured["controller"]["domains"] = {"127.0.0.1": "deny" if mode == "deny" else "allow"}
        inspected = await native.inspect(captured)
        policy = NetworkExecutionPolicy("actual-local", native, captured, inspected["effectivePolicy"], inspected["remoteLaunch"])
        sandbox = {"permissions": {"type": "managed", "file_system": {"type": "restricted", "entries": [
            {"path": {"type": "special", "value": {"kind": "root"}}, "access": "read"},
            {"path": {"type": "path", "path": tmp_path.as_uri()}, "access": "write"}]}, "network": "restricted"},
            "policyContext": {"cwd": tmp_path.as_uri(), "workspaceRoots": [tmp_path.as_uri()]},
            "windowsSandboxLevel": "elevated"}
        code = ("import os,socket,urllib.request\n"
            "try:\n"
            + (f" s=socket.create_connection(('127.0.0.1',{port}),2); print('BYPASS_OPEN'); s.close()\n" if mode == "direct"
                else f" o=urllib.request.build_opener(urllib.request.ProxyHandler({{'http':os.environ['HTTP_PROXY']}})); print(o.open('http://127.0.0.1:{port}/owned',timeout=5).read().decode())\n")
            + "except OSError as e: print('DENIED',type(e).__name__,str(e))\n")
        network_kwargs = {"managed_network_policy": policy,
            "process_observer": lambda process: borrowers.append(process.network_lease.clone())}
        if executor_kind == "source":
            lease = await policy.prepare_remote(environment_id="source", execution_id="actual-source",
                attribution_token="source-network-token")
            borrowers.append(lease)
            network_kwargs = {"enforce_managed_network": True, "network_proxy": lease.remote_launch,
                "network_decider": lease.decide, "process_id": "actual-source"}
        async with asyncio.timeout(20):
            process = await backend.start([sys.executable, "-I", "-S", "-u", "-c", code], tmp_path.as_uri(),
                sandbox=sandbox, network_observer=audit.append, **network_kwargs)
        chunks, after = [], 0
        try:
            async with asyncio.timeout(25):
                while True:
                    result = await process.read(after_seq=after, wait_ms=1000)
                    chunks.extend(chunk.chunk for chunk in result.chunks)
                    after = result.next_seq - 1
                    if result.closed:
                        break
        except TimeoutError:
            children = [(child.pid, child.name(), child.status()) for child in psutil.Process().children(recursive=True)]
            raise AssertionError({"output": b"".join(chunks), "audit": audit, "origin": received,
                "exited": process.exited, "exit_code": process.exit_code, "children": children})
        output = b"".join(chunks)
        await process.aclose()
        assert (process.process_id if executor_kind == "local" else "actual-source") in native.leases
        if mode == "allow":
            assert b"OWNED_OK" in output, output
            assert len(received) == 1
            assert any(event["decision"] == "allow" for event in audit)
        else:
            assert b"DENIED" in output and b"BYPASS_OPEN" not in output, output
            assert not received
        if mode == "deny":
            assert any(event["decision"] == "deny" for event in audit)
            event = next(event for event in audit if event["decision"] == "deny")
            assert native.invoke_sync("network/deniedMessage", {"event": event})["message"]
    finally:
        if manager is not None:
            await environment_lease.aclose()
            await manager.close()
        else:
            await backend.close()
        for lease in borrowers:
            await lease.aclose()
        await native.aclose()
        server.close()
        await server.wait_closed()
        await asyncio.gather(*clients)
