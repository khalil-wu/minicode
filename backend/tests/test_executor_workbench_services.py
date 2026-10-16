"""Actual Source workbench installs, Git delivery, cancellation and cleanup."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import shlex
import sys

import pytest

from backend.environments.errors import ExecutorDisconnected
from backend.services.workspace_api_service import capture_workspace_execution
from backend.terminal.executor_runner import ExecutorCommandRunner
from backend.tests.test_executor_editor_api import editor_server
from backend.tests.test_executor_tool_admission import executor_host
from backend.workspace.executor_commands import run_workbench_command, target_programs, workbench_policy
from backend.workspace.executor_editor import ExecutorEditorService
from backend.workspace.executor_paths import ExecutorPath
from backend.workspace.environment_setup import EnvironmentSetupRequest


pytestmark = pytest.mark.parametrize("executor_host", ["source"], indirect=True)


@asynccontextmanager
async def service_for(server):
    async with capture_workspace_execution(server.session, server.params["conversation_id"],
            environment_id=server.params["environment_id"]) as context:
        async with ExecutorEditorService(context, application_config=server.session.config) as service:
            yield service


async def target(server, *argv):
    async with service_for(server) as service:
        result = await service.run(list(argv))
    assert result.returncode == 0, result.stderr
    return result.stdout.decode("utf-8")


async def status(server, kind):
    response = await server.client.get("/api/workspace/" + kind, params=server.params)
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["environment_id"] == "target" and value["conversation_id"] == server.params["conversation_id"]
    assert value["session_id"] == server.session.session_id and value["registration_id"]
    return value


async def delivery(server, action, previous, **fields):
    response = await server.client.post("/api/workspace/git/delivery", params=server.params, json={
        "action": action, "expected_registration_id": previous["registration_id"],
        "expected_branch": previous.get("branch", ""), **fields})
    assert response.status_code == 200, response.text
    return response.json()


async def git_repository(server):
    await target(server, "git", "init", "-b", "main")
    for key, value in (("user.name", "Owned Source Test"), ("user.email", "owned-source@example.invalid"),
                       ("commit.gpgsign", "false"), ("core.hooksPath", str(server.host.workspace / "owned-empty-hooks"))):
        await target(server, "git", "config", "--local", key, value)
    (server.host.workspace / "staged.txt").write_text("actual staged target\n", encoding="utf-8")
    await target(server, "git", "add", "--", "staged.txt")


def offline_project(server, *, slow=False):
    script = ("const fs=require('fs');fs.writeFileSync('install-started.txt','TARGET');console.log('TARGET_INSTALL_STARTED');"
        + ("setTimeout(()=>fs.writeFileSync('install-finished.txt','DONE'),60000)" if slow else "fs.writeFileSync('install-finished.txt','DONE')"))
    (server.host.workspace / "package.json").write_text(json.dumps({"name": "owned-target-test", "version": "1.0.0",
        "private": True, "scripts": {"postinstall": "node -e " + json.dumps(script)}}), encoding="utf-8")
    (server.host.workspace / ".npmrc").write_text("offline=true\naudit=false\nfund=false\ncache=.owned-npm-cache\n", encoding="utf-8")


async def until(predicate):
    async with asyncio.timeout(15):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_source_environment_installs_offline_node_project_and_real_python_venv(editor_server):
    server = editor_server
    offline_project(server)
    initial = await status(server, "environment")
    assert initial["backend_python"] and initial["node"] and "node_dependencies" in initial["commands"]
    installed = await server.client.post("/api/workspace/environment/setup", params=server.params,
        json={"action": "node_dependencies", "expected_registration_id": initial["registration_id"]})
    assert installed.status_code == 200, installed.text
    events = [json.loads(line) for line in installed.text.splitlines()]
    assert events[-1] == {"exit_code": 0} and "TARGET_INSTALL_STARTED" in installed.text, installed.text
    assert (server.host.workspace / "install-finished.txt").read_text() == "DONE"
    created = await server.client.post("/api/workspace/environment/setup", params=server.params,
        json={"action": "python_venv", "expected_registration_id": initial["registration_id"]}, timeout=30)
    assert created.status_code == 200 and json.loads(created.text.splitlines()[-1]) == {"exit_code": 0}, created.text
    refreshed = await status(server, "environment")
    assert refreshed["project_python"] and refreshed["project_python"] != refreshed["backend_python"]
    assert (await target(server, refreshed["project_python"], "-c", "print('ACTUAL_VENV')")).splitlines() == ["ACTUAL_VENV"]
    for name in ("package.json", "package-lock.json", "install-started.txt", "install-finished.txt", ".venv"):
        assert not (server.view / name).exists()


@pytest.mark.asyncio
async def test_source_git_delivery_init_branch_unicode_commit_body_file_and_local_bare_push(editor_server, tmp_path, monkeypatch):
    server = editor_server
    initial = await status(server, "git/delivery")
    assert initial["is_git_repo"] is False
    await delivery(server, "init", initial)
    for key, value in (("user.name", "Owned Source Test"), ("user.email", "owned-source@example.invalid"),
                       ("commit.gpgsign", "false"), ("core.hooksPath", str(tmp_path / "empty-hooks"))):
        await target(server, "git", "config", "--local", key, value)
    current = await status(server, "git/delivery")
    await delivery(server, "branch", current, branch="codex/目标交付")
    (server.host.workspace / "staged.txt").write_text("staged on actual executor\n", encoding="utf-8")
    await target(server, "git", "add", "--", "staged.txt")
    captured = []
    from backend.environments.filesystem import ExecutorFileSystem
    original_write = ExecutorFileSystem.write_file
    async def observe_write(filesystem, path, contents, **kwargs):
        if ExecutorPath.parse(path).name.startswith("minicode-git-message-"):
            captured.append((filesystem.environment, path, contents))
        return await original_write(filesystem, path, contents, **kwargs)
    monkeypatch.setattr(ExecutorFileSystem, "write_file", observe_write)
    message = "目标提交：中文\n\n保留第二段和真实换行\n验证通过。\n"
    current = await status(server, "git/delivery")
    await delivery(server, "commit", current, message=message)
    assert (await target(server, "git", "log", "-1", "--format=%B")).rstrip("\n") == message.rstrip("\n")
    assert len(captured) == 1 and captured[0][2] == message.encode("utf-8")
    async with service_for(server) as service:
        assert captured[0][0] is service.environment and await service.metadata(captured[0][1]) is None
    bare = tmp_path / "owned-remote.git"
    await target(server, "git", "init", "--bare", str(bare))
    await target(server, "git", "remote", "add", "origin", str(bare))
    current = await status(server, "git/delivery")
    assert current["github"]["remote"] and not current["github"]["eligible"]
    await delivery(server, "push", current, remote="origin")
    assert (await target(server, "git", "--git-dir", str(bare), "rev-parse", "refs/heads/codex/目标交付")).strip() == (await target(server, "git", "rev-parse", "HEAD")).strip()
    assert not (server.view / ".git").exists() and not (server.view / "staged.txt").exists()


@pytest.mark.asyncio
async def test_source_setup_real_http_disconnect_closes_original_install_process(editor_server):
    server = editor_server
    offline_project(server, slow=True)
    initial = await status(server, "environment")
    async with service_for(server) as service:
        environment = service.environment
    async with server.client.stream("POST", "/api/workspace/environment/setup", params=server.params,
            json={"action": "node_dependencies", "expected_registration_id": initial["registration_id"]}) as response:
        assert response.status_code == 200
        marker = server.host.workspace / "install-started.txt"
        received = []
        async for line in response.aiter_lines():
            received.append(line)
            if marker.exists():
                break
        assert marker.exists(), "\n".join(received)
        handles = [handle for handle in environment.client.processes.values() if handle.accepted and not handle.closed]
        assert len(handles) == 1
        original = handles[0]
    await until(lambda: original.closed and original.released)
    assert not (server.host.workspace / "install-finished.txt").exists()
    assert not (server.view / "install-started.txt").exists()


@pytest.mark.asyncio
async def test_source_setup_full_output_queue_closes_original_process_pump_and_lease(editor_server, monkeypatch):
    from backend.workspace import executor_environment

    server = editor_server
    offline_project(server)
    package = json.loads((server.host.workspace / "package.json").read_text(encoding="utf-8"))
    script = "const fs=require('fs');fs.writeFileSync('noise-started.txt','TARGET');setInterval(()=>process.stdout.write('x'.repeat(65536)),5)"
    package["scripts"]["postinstall"] = "node -e " + json.dumps(script)
    (server.host.workspace / "package.json").write_text(json.dumps(package), encoding="utf-8")
    queues, runners = [], []
    original_queue = asyncio.Queue
    class ObservedQueue(original_queue):
        def __init__(self, maxsize=0):
            super().__init__(maxsize)
            if maxsize == 16:
                queues.append(self)
    class ObservedRunner(ExecutorCommandRunner):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs); runners.append(self)
    monkeypatch.setattr(executor_environment.asyncio, "Queue", ObservedQueue)
    monkeypatch.setattr("backend.workspace.executor_commands.ExecutorCommandRunner", ObservedRunner)
    async with service_for(server) as service:
        current = await executor_environment.executor_environment_status(service)
        response = await executor_environment.executor_environment_setup(service,
            EnvironmentSetupRequest(action="node_dependencies", expected_registration_id=current["registration_id"]))
        iterator = response.body_iterator
        try:
            await anext(iterator)
            # Pause the real consumer while the actual target keeps writing.
            await until(lambda: bool(queues) and queues[0].full())
            assert queues[0].qsize() == 16
            runner, = runners
            assert runner.handle.accepted and not runner.lease.released
            assert (server.host.workspace / "noise-started.txt").read_text() == "TARGET"
        finally:
            await asyncio.wait_for(iterator.aclose(), 15)
        assert runner.closed and runner.handle.closed and runner.handle.released and runner.lease.released
        assert runner.pump is not None and runner.pump.done()
        assert not (server.view / "noise-started.txt").exists()


@pytest.mark.asyncio
async def test_rebound_same_id_registration_rejects_status_from_the_original_attachment(editor_server, tmp_path):
    server = editor_server
    initial = await status(server, "git/delivery")
    selections = server.host.owner.selections()
    home = tmp_path / "replacement-home"; home.mkdir()
    replacement = server.host.manager.upsert({"id": "target", "program": os.environ["MINICODE_SOURCE_EXECUTOR"],
        "args": ["exec-server", "--listen", "stdio", "--concurrent-requests", "4"],
        "cwd": str(server.host.workspace), "env": {"CODEX_HOME": str(home)}})
    await server.host.owner.update_selections(())
    await server.host.owner.update_selections(selections)
    current = await status(server, "git/delivery")
    assert current["registration_id"] != initial["registration_id"]
    rejected = await server.client.post("/api/workspace/git/delivery", params=server.params,
        json={"action": "init", "expected_registration_id": initial["registration_id"]})
    assert rejected.status_code == 409 and "changed" in rejected.text
    assert not (server.host.workspace / ".git").exists() and not (server.view / ".git").exists()
    assert not replacement.client.processes


@pytest.mark.asyncio
async def test_cleanup_callback_failure_retains_original_lease_until_real_resource_is_removed(editor_server):
    server = editor_server
    async with service_for(server) as service:
        resource = service.cwd.joinpath("owned-cleanup.txt")
        await service.write_bytes(resource, b"original resource")
        calls = []
        async def remove():
            calls.append(service.environment)
            if len(calls) == 1:
                raise ExecutorDisconnected("owned callback fault; no replacement environment may clean this resource")
            await service.remove(resource)
        runner = ExecutorCommandRunner(await workbench_policy(service, timeout=10), cleanup_callbacks=[remove])
        with pytest.raises(ExecutorDisconnected) as failure:
            await runner.run("owned cleanup command", cwd=service.cwd.uri,
                argv=[sys.executable, "-I", "-S", "-c", "print('OWNED_CALLBACK_RESOURCE')"])
        assert failure.value.cleanup_pending and runner.handle.closed and not runner.lease.released
        assert await service.metadata(resource) is not None
        assert await runner.cleanup()
        assert runner.closed and runner.lease.released and await service.metadata(resource) is None
        assert calls == [service.environment, service.environment]


@pytest.mark.asyncio
async def test_concurrent_cleanup_and_late_callback_execute_once_on_original_owner(editor_server, monkeypatch):
    async with service_for(editor_server) as service:
        entered, release = asyncio.Event(), asyncio.Event()
        callbacks, closes = [], []
        async def first():
            callbacks.append("first"); entered.set(); await release.wait()
            await service.write_bytes(service.cwd.joinpath("first-cleanup.txt"), b"original first")
        async def late():
            callbacks.append("late")
            await service.write_bytes(service.cwd.joinpath("late-cleanup.txt"), b"original late")
        runner = ExecutorCommandRunner(await workbench_policy(service, timeout=10), cleanup_callbacks=[first])
        original_close = runner.lease.aclose
        async def close_original():
            closes.append(runner.lease.environment)
            await original_close()
        monkeypatch.setattr(runner.lease, "aclose", close_original)
        first_cleanup = asyncio.create_task(runner.cleanup())
        await entered.wait()
        second_cleanup = asyncio.create_task(runner.cleanup())
        add_late = asyncio.create_task(runner.add_cleanup_callback(late))
        await asyncio.sleep(0)
        assert callbacks == ["first"] and not second_cleanup.done() and not add_late.done()
        assert not runner.lease.released
        release.set()
        assert await first_cleanup and await second_cleanup
        await add_late
        assert callbacks == ["first", "late"] and closes == [service.environment]
        assert runner.closed and runner.lease.released and not runner.cleanup_callbacks
        assert await service.read_bytes(service.cwd.joinpath("first-cleanup.txt")) == b"original first"
        assert await service.read_bytes(service.cwd.joinpath("late-cleanup.txt")) == b"original late"


@pytest.mark.asyncio
async def test_target_batch_program_preserves_literal_argv_and_nonzero_exit(editor_server):
    server = editor_server
    directory = server.host.workspace / "owned tool's folder"
    directory.mkdir()
    script = directory / "record_argv.py"
    receipt = directory / "actual argv.json"
    script.write_text("import json,sys\nfrom pathlib import Path\n"
        "Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:],ensure_ascii=False),encoding='utf-8')\n"
        "print('ACTUAL_TARGET_ARGV',flush=True)\nraise SystemExit(37)\n", encoding="utf-8")
    windows = server.host.environments.primary().peer_info["platformOs"] == "windows"
    program = directory / ("actual tool's.cmd" if windows else "actual tool's.sh")
    if windows:
        program.write_bytes((f'@echo off\r\n"{sys.executable}" -I -S "%~dp0record_argv.py" %*\r\nexit /b %errorlevel%\r\n').encode())
    else:
        program.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -I -S {shlex.quote(str(script))} "$@"\n', encoding="utf-8")
        program.chmod(0o700)
    arguments = ["中文参数", "two words", "single'quote", "amp&literal", "pipe|literal", "greater>literal"]
    async with service_for(server) as service:
        programs = await target_programs(service)
        result = await run_workbench_command(service, [str(program), str(receipt), *arguments], programs=programs, allow_failure=True)
        assert result.exit_code == 37, {"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.exit_code}
    assert json.loads(receipt.read_text(encoding="utf-8")) == arguments
    assert not (server.view / "actual argv.json").exists()


GATED_SOURCE_PROXY = r'''import json,os,subprocess,sys,threading,time
from pathlib import Path
source,accepted,gate,trace=sys.argv[1:]
child=subprocess.Popen([source,'exec-server','--listen','stdio','--concurrent-requests','4'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=sys.stderr,
 creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
def forward():
 for line in child.stdout:
  sys.stdout.buffer.write(line);sys.stdout.buffer.flush()
thread=threading.Thread(target=forward);thread.start()
for line in sys.stdin.buffer:
 message=json.loads(line);method=message.get('method');path=message.get('params',{}).get('path','')
 if method in ('fs/writeFile','fs/remove') and 'minicode-git-message-' in path:
  with Path(trace).open('a',encoding='utf-8') as output:output.write(json.dumps({'method':method,'path':path})+'\n')
 if method=='fs/writeFile' and 'minicode-git-message-' in path:
  Path(accepted).write_text(json.dumps({'method':method,'path':path,'id':message['id']}),encoding='utf-8')
  while not Path(gate).exists():time.sleep(0.01)
 child.stdin.write(line);child.stdin.flush()
child.stdin.close();child.wait();thread.join()
'''


@pytest.mark.asyncio
async def test_actual_http_abort_after_peer_accepts_message_write_waits_for_write_and_original_cleanup(editor_server, tmp_path, monkeypatch):
    server = editor_server
    await git_repository(server)
    proxy, accepted, gate, trace = [tmp_path / name for name in ("source_proxy.py", "write-accepted.json", "allow-write", "source-fs-trace.jsonl")]
    proxy.write_text(GATED_SOURCE_PROXY, encoding="utf-8")
    home = tmp_path / "gated-source-home"; home.mkdir()
    selections = server.host.owner.selections()
    original = server.host.manager.upsert({"id": "target", "program": sys.executable,
        "args": ["-I", "-S", str(proxy), os.environ["MINICODE_SOURCE_EXECUTOR"], str(accepted), str(gate), str(trace)],
        "cwd": str(server.host.workspace), "env": {"CODEX_HOME": str(home)}})
    await server.host.owner.update_selections(())
    await server.host.owner.update_selections(selections)
    current = await status(server, "git/delivery")
    runners, cleanup_entered = [], asyncio.Event()
    class ObservedRunner(ExecutorCommandRunner):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs); runners.append(self)
        async def cleanup(self):
            if self.cleanup_callbacks:
                cleanup_entered.set()
            return await super().cleanup()
    monkeypatch.setattr("backend.workspace.executor_commands.ExecutorCommandRunner", ObservedRunner)
    request = server.client.build_request("POST", "/api/workspace/git/delivery", params=server.params,
        json={"action": "commit", "expected_branch": current["branch"], "expected_registration_id": current["registration_id"], "message": "不得提交的取消正文\n第二行"})
    reader, writer = await asyncio.open_connection(request.url.host, request.url.port)
    body = request.content
    writer.write(b"POST " + request.url.raw_path + b" HTTP/1.1\r\nHost: " + request.url.netloc + b"\r\nContent-Type: application/json\r\nContent-Length: "
        + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
    await writer.drain()
    try:
        await until(accepted.exists)
        receipt = json.loads(accepted.read_text(encoding="utf-8"))
        message_path = ExecutorPath.parse(receipt["path"]).to_local_path()
        assert not message_path.exists()  # The protocol peer accepted it; native Source has not received the gated write.
        writer.close(); await writer.wait_closed()  # Actual TCP disconnect, not cancellation of the server task.
        await asyncio.wait_for(cleanup_entered.wait(), 10)
        runner, = runners
        assert not runner.lease.released and not runner.closed and runner.cleanup_callbacks
        replacement = server.host.manager.upsert({"id": "target", "program": "replacement-must-never-connect"})
        assert runner.environment is original and original.retired
        gate.write_text("release actual Source write", encoding="utf-8")
        await until(lambda: runner.closed)
        assert runner.lease.released and runner.handle is None and not message_path.exists()
        assert [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()] == [
            {"method": "fs/writeFile", "path": receipt["path"]}, {"method": "fs/remove", "path": receipt["path"]}]
        assert replacement.client.connect_task is None
        async with service_for(server) as service:
            result = await service.run(["git", "rev-parse", "--verify", "HEAD"])
            assert result.returncode != 0
        assert not (server.view / ".git").exists()
    finally:
        gate.write_text("owned cleanup release", encoding="utf-8")
        if not writer.is_closing():
            writer.close(); await writer.wait_closed()
