"""Launch contracts everywhere; real namespace/proxy probes only on Linux.

The declared-package fixture is never executed. It tests Python admission and
argv ownership, not Linux isolation. The Linux probes require the actual newly
compiled pinned runtime and the host kernel's user/network namespaces.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tomllib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import psutil
import pytest

from backend.network.native import SOURCE_COMMIT
from backend.network.policy import NetworkExecutionPolicy
from backend.sandbox.linux_native import command, discover_runtime, permission_profile
from backend.sandbox.policy import (FileSystemAccessMode as Access, FileSystemPath as Selector,
    FileSystemSandboxEntry as Entry, FileSystemSandboxPolicy as Filesystem, FileSystemSpecialPath as Special,
    PermissionProfile, SandboxPolicy)
from backend.sandbox.runner import SandboxRunner, SandboxUnavailableError
from backend.tests.test_managed_network_runtime import runtime, source_policy
from backend.workspace.executor_local import LocalExecutorProcess, LocalProcessBackend


@pytest.fixture
def declared_package(tmp_path):
    binary = tmp_path / "declared-linux-runtime"
    binary.write_bytes(b"Not an executable test peer; launch contract only.\n")
    binary.chmod(0o755)
    manifest = {"identity": "minicode-network", "protocol": 1, "sourceCommit": SOURCE_COMMIT,
                "linux_sandbox_routing_protocol": 1}
    (tmp_path / "runtime.json").write_text(json.dumps(manifest), encoding="utf-8")
    return binary


def test_old_native_package_cannot_claim_linux_routing(declared_package):
    manifest_path = declared_package.parent / "runtime.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["linux_sandbox_routing_protocol"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    binary, reason = discover_runtime(declared_package)
    assert binary is None and "does not contain" in reason
    manifest_path.unlink()
    assert discover_runtime(declared_package)[0] is None


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_platform_build_stages_only_its_real_entrypoint_and_dependencies(platform, tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[2] / "desktop/scripts/prepare-network-runtime.py"
    specification = importlib.util.spec_from_file_location("linux_routing_packaging_contract", script)
    builder = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(builder)
    monkeypatch.setattr(builder.sys, "platform", platform)
    project = tmp_path / "staged-bridge"
    inputs = builder.prepare_bridge(project)
    manifest = tomllib.loads((project / "Cargo.toml").read_text(encoding="utf-8"))
    assert (project / "src/main.rs").read_bytes() == (builder.BRIDGE / "src/main.rs").read_bytes()
    if platform == "linux":
        assert manifest["target"]['cfg(target_os = "linux")']["dependencies"]["codex-linux-sandbox"] == {"path": "../linux-sandbox"}
        assert manifest["bin"] == [{"name": "minicode-network", "path": "src/linux_entry.rs"}]
        assert (project / "src/linux_entry.rs").read_bytes() == (builder.BRIDGE / "linux/entry.rs").read_bytes()
        assert project / "src/linux_entry.rs" in inputs
    else:
        assert (project / "Cargo.toml").read_bytes() == (builder.BRIDGE / "Cargo.toml").read_bytes()
        assert "codex-linux-sandbox" not in str(manifest) and not (project / "src/linux_entry.rs").exists()


def test_materialized_profile_retains_all_roots_denies_and_protected_metadata(tmp_path):
    first, second, temporary = [tmp_path / name for name in ("first", "second", "temporary")]
    for path in (first, second, temporary):
        path.mkdir()
    denied = first / ".git"
    policy = SandboxPolicy(permission_profile=PermissionProfile.managed(Filesystem.restricted([
        Entry(Selector.special(Special.ROOT), Access.READ),
        Entry(Selector.special(Special.PROJECT_ROOTS), Access.WRITE),
        Entry(Selector.special(Special.TMPDIR), Access.WRITE),
        Entry(Selector.path(denied), Access.DENY),
    ])), workspace_roots=(first, second), temporary_directories=(temporary,))
    wire = permission_profile(policy.resolve(cwd=first))
    concrete = {item["path"]["path"]: item["access"] for item in wire["file_system"]["entries"]
                if item["path"]["type"] == "path"}
    assert concrete[str(first)] == concrete[str(second)] == concrete[str(temporary)] == "write"
    assert concrete[str(denied)] == "deny"
    assert concrete[str(second / ".git")] == "read"
    assert all(item["path"].get("value", {}).get("kind") not in {"project_roots", "tmpdir"}
               for item in wire["file_system"]["entries"])


@pytest.mark.parametrize("bypass", [False, True])
def test_managed_launch_keeps_namespace_with_enabled_network_and_original_argv(
        declared_package, tmp_path, monkeypatch, bypass):
    monkeypatch.setattr("backend.sandbox.runner.sys.platform", "linux")
    monkeypatch.setattr("backend.sandbox.runner._bubblewrap_capability", lambda: (True, ""))
    policy = (SandboxPolicy.bypass() if bypass else SandboxPolicy(
        workspace_root=tmp_path, writable_roots=(tmp_path,), allow_network=True))
    policy = replace(policy, enforce_managed_network=True, use_legacy_landlock=True,
        linux_sandbox_executable=declared_package,
        managed_network={"allow_local_binding": False, "allow_unix_sockets": [], "loopback_ports": [31901]})
    runner = SandboxRunner(policy)
    argv = ["/usr/bin/python3", "-c", "print('literal $() & | >')", "中文 argument"]
    wrapped, capability = runner.prepare_command("unused shell string", cwd=tmp_path, argv=argv)
    assert capability.available and capability.backend == "linux-managed-proxy" and capability.network_isolated
    assert capability.filesystem_isolated is not bypass
    assert wrapped[0] == str(declared_package) and wrapped[1] == "--sandbox-policy-cwd"
    assert wrapped[wrapped.index("--") + 1:] == argv
    assert "--use-legacy-landlock" not in wrapped and "--proxy-route-spec" not in wrapped
    assert json.loads(wrapped[wrapped.index("--managed-network") + 1]) == dict(policy.managed_network)
    assert runner._bubblewrap_mount_targets is None


def test_unavailable_kernel_namespace_never_selects_an_unmanaged_backend(declared_package, tmp_path, monkeypatch):
    monkeypatch.setattr("backend.sandbox.runner.sys.platform", "linux")
    monkeypatch.setattr("backend.sandbox.runner._bubblewrap_capability", lambda: (False, "owned kernel probe rejected user namespace"))
    runner = SandboxRunner(replace(SandboxPolicy.bypass(), enforce_managed_network=True,
        linux_sandbox_executable=declared_package))
    capability = runner.capability(cwd=tmp_path)
    assert not capability.available and not capability.network_isolated
    assert capability.backend == "linux-managed-proxy" and "owned kernel probe" in capability.reason
    with pytest.raises(SandboxUnavailableError, match="owned kernel probe"):
        runner.prepare_command("must not run", cwd=tmp_path)


@pytest.mark.asyncio
async def test_local_admission_keeps_actual_native_owner_and_skips_windows_readiness(
        declared_package, tmp_path, monkeypatch):
    monkeypatch.setattr("backend.workspace.executor_local.sys.platform", "linux")
    monkeypatch.setattr("backend.sandbox.runner._bubblewrap_capability", lambda: (True, ""))
    def windows_readiness_must_not_run(*_args):
        raise AssertionError("Windows readiness is not a Linux launch prerequisite")
    monkeypatch.setattr("backend.sandbox.windows_native.capture_local_network_prerequisite", windows_readiness_must_not_run)
    native = SimpleNamespace(binary=declared_package)
    lease = SimpleNamespace(env={"HTTP_PROXY": "http://127.0.0.1:31901", "CODEX_NETWORK_PROXY_ATTRIBUTION": "original-token"},
        sandbox_context={"loopback_ports": [31901]}, restricting_sid=None, network_audit=[],
        flush_audit=AsyncMock(), aclose=AsyncMock())
    policy = SimpleNamespace(runtime=native, environment_id="original-local", prepare_local=AsyncMock(return_value=lease))
    observed = []
    async def at_spawn(process, argv, cwd, arg0):
        observed.append(process.runner._policy)
        native.binary = tmp_path / "replacement-must-not-be-selected"
        assert process.runner._policy.linux_sandbox_executable == declared_package
        assert process.runner._build_env()["CODEX_NETWORK_PROXY_ATTRIBUTION"] == "original-token"
        raise RuntimeError("owned spawn boundary for the platform-independent contract")
    monkeypatch.setattr(LocalExecutorProcess, "_start", at_spawn)
    backend = LocalProcessBackend()
    with pytest.raises(RuntimeError, match="owned spawn boundary"):
        await backend.start(["not-executed"], tmp_path.as_uri(), sandbox=None, managed_network_policy=policy)
    assert len(observed) == 1 and not backend.processes
    assert observed[0].managed_network == lease.sandbox_context
    assert observed[0].captured_env == lease.env and observed[0].enforce_managed_network
    lease.flush_audit.assert_awaited_once()
    lease.aclose.assert_awaited_once()


LINUX_ONLY = pytest.mark.skipif(sys.platform != "linux", reason="Requires actual Linux user/network namespaces and the freshly compiled native runtime")


@LINUX_ONLY
def test_compiled_native_binary_dispatches_source_linux_helper_before_proxy_runtime():
    native = runtime()
    binary, reason = discover_runtime(native.binary)
    assert binary is not None, reason
    for prefix in ([], ["--verify-fd-mount", "999999:/not-executed-by-help"]):
        result = subprocess.run([str(binary), *prefix, "--sandbox-policy-cwd", "/", "--help"],
            capture_output=True, stdin=subprocess.DEVNULL, timeout=10)
        assert result.returncode == 0, result.stderr
        assert b"--sandbox-policy-cwd" in result.stdout and b"--permission-profile" not in result.stderr


PROBE = r'''
import json, os, socket, struct, sys, time
from urllib.parse import urlsplit
mode, port = sys.argv[1], int(sys.argv[2])
meta = {'http_proxy':os.environ['HTTP_PROXY'], 'all_proxy':os.environ['ALL_PROXY'],
        'attribution_visible':'CODEX_NETWORK_PROXY_ATTRIBUTION' in os.environ,
        'netns':os.readlink('/proc/self/ns/net')}
meta['socket_fds'] = []
for fd in os.listdir('/proc/self/fd'):
    try: target = os.readlink('/proc/self/fd/'+fd)
    except FileNotFoundError: continue
    if target.startswith('socket:'): meta['socket_fds'].append(fd)
try:
    check = socket.socket(socket.AF_UNIX)
    check.close(); meta['unix_socket_blocked'] = False
except OSError: meta['unix_socket_blocked'] = True
if len(sys.argv) > 3:
    try:
        with open(sys.argv[3],'w') as file: file.write('BYPASS_WRITE')
        meta['outside_write_blocked'] = False
    except OSError: meta['outside_write_blocked'] = True
    with open('namespace-owned.txt','w') as file: file.write('TARGET_WRITE')
print(json.dumps(meta), flush=True)
def exact(sock, count):
    result = b''
    while len(result) < count:
        chunk = sock.recv(count-len(result))
        if not chunk: raise EOFError('proxy closed its handshake')
        result += chunk
    return result
def headers(sock):
    result = b''
    while not result.endswith(b'\r\n\r\n'):
        result += exact(sock,1)
    return result
try:
    if mode == 'direct':
        with socket.create_connection(('127.0.0.1', port),2):
            print('BYPASS_OPEN',flush=True)
    else:
        proxy = urlsplit(os.environ['ALL_PROXY' if mode == 'socks' else 'HTTP_PROXY'])
        with socket.create_connection((proxy.hostname,proxy.port),3) as sock:
            sock.settimeout(5)
            if mode == 'socks':
                sock.sendall(b'\x05\x01\x00')
                assert exact(sock,2) == b'\x05\x00'
                sock.sendall(b'\x05\x01\x00\x01\x7f\x00\x00\x01'+struct.pack('!H',port))
                reply = exact(sock,4)
                if reply[1] != 0: raise OSError('SOCKS_DENIED '+str(reply[1]))
                size = {1:4,4:16}.get(reply[3])
                if size is None: size = exact(sock,1)[0]
                exact(sock,size+2)
            elif mode == 'connect':
                sock.sendall(('CONNECT 127.0.0.1:%d HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n' % (port,port)).encode())
                reply = headers(sock)
                if b' 200 ' not in reply.split(b'\r\n')[0]: raise OSError(reply.decode())
            if mode == 'hold':
                print('OWNED_BRIDGE_READY',flush=True)
                time.sleep(60)
            else:
                target = ('http://127.0.0.1:%d/owned' % port) if mode == 'http' else '/owned'
                sock.sendall(('GET %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nConnection: close\r\n\r\n' % (target,port)).encode())
                reply = b''
                while chunk := sock.recv(4096): reply += chunk
                print(reply.decode(),flush=True)
except OSError as error:
    print('DENIED',type(error).__name__,str(error),flush=True)
'''


def launch_scope(root):
    return {"permissions": {"type": "managed", "file_system": {"type": "restricted", "entries": [
        {"path": {"type": "special", "value": {"kind": "root"}}, "access": "read"},
        {"path": {"type": "path", "path": root.as_uri()}, "access": "write"}]}, "network": "restricted"},
        "policyContext": {"cwd": root.as_uri(), "workspaceRoots": [root.as_uri()]}, "windowsSandboxLevel": "disabled"}


async def prepared_policy(native, *, denied=False):
    captured = source_policy()
    captured["controller"].update(domains={"127.0.0.1": "deny" if denied else "allow"}, enable_socks5=True)
    inspected = await native.inspect(captured)
    return NetworkExecutionPolicy("owned-linux", native, captured, inspected["effectivePolicy"], inspected["remoteLaunch"])


async def output(process):
    chunks, cursor = [], 0
    async with asyncio.timeout(20):
        while True:
            value = await process.read(after_seq=cursor, wait_ms=1000)
            chunks.extend(chunk.chunk for chunk in value.chunks)
            cursor = value.next_seq - 1
            if value.closed:
                assert value.exit_code == 0 and not value.failure, b"".join(chunks)
                return b"".join(chunks)


@LINUX_ONLY
@pytest.mark.asyncio
@pytest.mark.parametrize("mode,denied", [("http",False),("connect",False),("socks",False),("http",True),("socks",True),("direct",False)])
async def test_actual_linux_namespace_routes_only_attributed_proxy_traffic(mode, denied, tmp_path, record_property):
    native, backend, received, clients, audit = runtime(), LocalProcessBackend(), [], set(), []
    async def origin(reader, writer):
        task=asyncio.current_task(); clients.add(task)
        try:
            received.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 8\r\nConnection: close\r\n\r\nOWNED_OK")
            await writer.drain()
        finally:
            writer.close(); await writer.wait_closed(); clients.remove(task)
    server = await asyncio.start_server(origin,"127.0.0.1",0)
    port = server.sockets[0].getsockname()[1]
    outside = tmp_path.parent / (tmp_path.name + "-outside-owned.txt")
    outside.write_text("ORIGINAL",encoding="utf-8")
    try:
        policy = await prepared_policy(native, denied=denied)
        process = await backend.start([sys.executable,"-I","-S","-u","-c",PROBE,mode,str(port),str(outside)],tmp_path.as_uri(),
            sandbox=launch_scope(tmp_path),managed_network_policy=policy,network_observer=audit.append)
        lease=process.network_lease
        result=await output(process)
        await lease.flush_audit()
        record_property("actual_probe_output",result.decode())
        record_property("actual_native_audit",json.dumps(audit))
        meta=json.loads(result.splitlines()[0])
        assert process.sandbox_backend == "linux-managed-proxy" and process.sandbox_type == "linuxSeccomp"
        assert meta["netns"] != os.readlink("/proc/self/ns/net")
        assert not meta["attribution_visible"]
        assert not meta["socket_fds"] and meta["unix_socket_blocked"] and meta["outside_write_blocked"],meta
        assert outside.read_text(encoding="utf-8") == "ORIGINAL"
        assert (tmp_path / "namespace-owned.txt").read_text(encoding="utf-8") == "TARGET_WRITE"
        assert meta["http_proxy"] != lease.env["HTTP_PROXY"] and meta["all_proxy"] != lease.env["ALL_PROXY"]
        if denied or mode == "direct":
            assert not received and b"OWNED_OK" not in result and b"BYPASS_OPEN" not in result, result
            assert b"DENIED" in result or b" 403 " in result, result
        else:
            assert b"OWNED_OK" in result and len(received) == 1, result
        if mode != "direct":
            assert any(event["decision"] == ("deny" if denied else "allow") for event in audit), audit
            assert all(event["executionId"] == process.process_id and event["environmentId"] == "owned-linux" for event in audit)
        await process.aclose()
        assert process.released and process.process_id not in native.leases
    finally:
        await backend.close(); await native.aclose()
        server.close(); await server.wait_closed()
        for task in tuple(clients): task.cancel()
        await asyncio.gather(*tuple(clients),return_exceptions=True)


@LINUX_ONLY
@pytest.mark.asyncio
async def test_actual_linux_bridge_tree_and_original_proxy_lease_close_after_repeated_cancellation(tmp_path, record_property):
    native, backend, cursor, chunks = runtime(), LocalProcessBackend(), 0, []
    try:
        policy = await prepared_policy(native)
        process = await backend.start([sys.executable,"-I","-S","-u","-c",PROBE,"hold","1"],tmp_path.as_uri(),
            sandbox=launch_scope(tmp_path),managed_network_policy=policy)
        async with asyncio.timeout(15):
            while b"OWNED_BRIDGE_READY" not in b"".join(chunks):
                value=await process.read(after_seq=cursor,wait_ms=1000)
                chunks.extend(chunk.chunk for chunk in value.chunks); cursor=value.next_seq-1
                assert not value.closed,b"".join(chunks)
        original=process.network_lease
        borrower=original.clone()
        owned=[psutil.Process(process.process.pid),*psutil.Process(process.process.pid).children(recursive=True)]
        assert len(owned) >= 3, "Source helper, bridges and sandbox child must be real processes"
        record_property("owned_processes",json.dumps([{"pid":item.pid,"name":item.name()} for item in owned]))
        waiter=asyncio.create_task(process.aclose())
        await asyncio.sleep(0)
        waiter.cancel(); await asyncio.sleep(0); waiter.cancel()
        await asyncio.gather(waiter,return_exceptions=True)
        await process.aclose()
        assert original.closed and not borrower.closed and process.process_id in native.leases
        assert not any(item.is_running() and item.status()!=psutil.STATUS_ZOMBIE for item in owned)
        await borrower.aclose()
        assert process.process_id not in native.leases and not backend.processes
    finally:
        await backend.close(); await native.aclose()
