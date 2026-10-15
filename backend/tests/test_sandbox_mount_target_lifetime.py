"""Private bwrap masks retain exact host targets through the actual child lifetime."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.sandbox import SandboxPolicy, SandboxRunner
from backend.sandbox.mount_targets import BubblewrapMountTargets
from backend.sandbox.policy import FileSystemAccessMode, FileSystemPath, FileSystemSandboxEntry, FileSystemSandboxPolicy, FileSystemSpecialPath, PermissionProfile
from backend.sandbox.runner import _bubblewrap_command
from backend.tests.git_sandbox_policy import readable_host_git_policy


@pytest.fixture(autouse=True)
def isolated_mount_registry(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.sandbox.mount_targets.MOUNT_TARGET_REGISTRY_ROOT", tmp_path / "mount-registry")


def _policy(root: Path, generic_count: int = 0):
    missing = tuple(root / f"ordinary-readonly-{index}" for index in range(generic_count))
    return readable_host_git_policy(SandboxPolicy(workspace_root=root, writable_roots=(root,), readable_roots=missing)), missing


def test_private_masks_keep_missing_metadata_directories_and_generic_file_data(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    original = root / ".mcp.json"
    original.write_text('{"real":"user-content"}', encoding="utf-8")
    empty = root / ".gitconfig"
    empty.touch()
    original_inode, empty_inode = original.stat().st_ino, empty.stat().st_ino
    policy, missing = _policy(root, 12)
    runner = SandboxRunner(policy)
    try:
        ready = runner._prepare_bubblewrap_mount_targets(policy.resolve(cwd=root))
        targets = runner._bubblewrap_mount_targets
        assert ready is not None
        assert (root / ".gitmodules").is_dir()
        assert all(path.is_file() for path in missing)
        argv = shlex.split(_bubblewrap_command("true", policy.resolve(cwd=root), ready_path=ready,
            mount_path_overrides=runner._synthetic_mount_overrides, private_mount_targets=targets))
        assert ["--tmpfs", str(root / ".gitmodules")] in [argv[index:index + 2] for index in range(len(argv) - 1)]
        file_mounts = [argv[index:index + 3] for index, arg in enumerate(argv) if arg == "--ro-bind-data"]
        assert {row[2] for row in file_mounts} == {str(path) for path in (*missing, empty)}
        assert len(targets.preserved_fds) == 13 and max(targets.preserved_fds) >= 10
        for descriptor in targets.preserved_fds:
            assert os.read(descriptor, 1) == b""
    finally:
        runner._cleanup_sandbox_setup_state()
    assert not (root / ".gitmodules").exists()
    assert all(not path.exists() for path in missing)
    assert original.read_text(encoding="utf-8") == '{"real":"user-content"}'
    assert original.stat().st_ino == original_inode
    assert empty.is_file() and empty.stat().st_ino == empty_inode and empty.read_bytes() == b""


def test_generic_missing_deny_file_uses_zero_permission_bind_data(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    denied = root / "ordinary-denied-file"
    policy = readable_host_git_policy(SandboxPolicy(workspace_root=root, writable_roots=(root,), denied_roots=(denied,)))
    runner = SandboxRunner(policy)
    try:
        ready = runner._prepare_bubblewrap_mount_targets(policy.resolve(cwd=root))
        targets = runner._bubblewrap_mount_targets
        assert targets.masks[denied] == "file" and denied.is_file()
        argv = shlex.split(_bubblewrap_command("true", policy.resolve(cwd=root), ready_path=ready,
            mount_path_overrides=runner._synthetic_mount_overrides, private_mount_targets=targets))
        masked = next(argv[index:index + 5] for index in range(len(argv) - 4)
            if argv[index:index + 3] == ["--perms", "000", "--ro-bind-data"] and argv[index + 4] == str(denied))
        descriptor = int(masked[3])
        assert descriptor in targets.preserved_fds and os.read(descriptor, 1) == b""
    finally:
        runner._cleanup_sandbox_setup_state()
    assert not denied.exists()


def _registry_parent_denied_policy(root, targets):
    return SandboxPolicy(workspace_root=root, protect_workspace_metadata=False,
        permission_profile=PermissionProfile.managed(FileSystemSandboxPolicy.restricted([
            FileSystemSandboxEntry(FileSystemPath.special(FileSystemSpecialPath.ROOT), FileSystemAccessMode.WRITE),
            FileSystemSandboxEntry(FileSystemPath.path(targets.registry_root.parent), FileSystemAccessMode.DENY),
            FileSystemSandboxEntry(FileSystemPath.path(root), FileSystemAccessMode.WRITE),
        ])))


def test_registry_protection_precedes_denied_parent_mask_without_own_targets(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    targets = BubblewrapMountTargets()
    assert not targets.masks
    policy = _registry_parent_denied_policy(root, targets)
    argv = shlex.split(_bubblewrap_command("true", policy.resolve(cwd=root), private_mount_targets=targets))
    registry_bind = next(index for index in range(len(argv) - 2)
        if argv[index:index + 3] == ["--ro-bind", str(targets.registry_root), str(targets.registry_root)])
    parent_deny = next(index for index in range(len(argv) - 1)
        if argv[index:index + 2] == ["--tmpfs", str(targets.registry_root.parent)])
    assert registry_bind < parent_deny
    assert not any(argv[index:index + 3] == ["--ro-bind", str(targets.registry_root), str(targets.registry_root)]
        for index in range(parent_deny + 1, len(argv) - 2))


def test_namespace_ready_and_pending_cleanup_keep_the_same_mount_target_owner(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    policy, missing = _policy(root, 1)
    runner = SandboxRunner(policy)
    ready = runner._prepare_bubblewrap_mount_targets(policy.resolve(cwd=root))
    ready.write_text("ready", encoding="utf-8")
    identities = {path: path.stat().st_ino for path in runner._bubblewrap_mount_targets.masks}
    process = SimpleNamespace(pid=1234, returncode=None)
    runner.process = process
    reaped = False

    async def terminate(_process):
        return reaped

    monkeypatch.setattr("backend.sandbox.runner.terminate_process_tree", terminate)

    async def scenario():
        nonlocal reaped
        await runner._await_sandbox_ready(process)
        assert ready.exists()
        assert runner._bubblewrap_mount_targets is not None
        assert not await runner.cleanup()
        assert runner.process is process
        assert {path: path.stat().st_ino for path in identities} == identities
        reaped = True
        assert await runner.cleanup()
        assert runner.process is None

    asyncio.run(scenario())
    assert not ready.exists()
    assert not (root / ".gitmodules").exists()
    assert not missing[0].exists()


def test_overlapping_command_owners_keep_targets_until_the_last_child_settles(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    policy, missing = _policy(root, 1)
    first, second = SandboxRunner(policy), SandboxRunner(policy)
    try:
        first._prepare_bubblewrap_mount_targets(policy.resolve(cwd=root))
        before = (root / ".gitmodules").stat().st_ino, missing[0].stat().st_ino
        second._prepare_bubblewrap_mount_targets(policy.resolve(cwd=root))
        assert second._bubblewrap_mount_targets.masks[missing[0]] == "file"
        first._cleanup_sandbox_setup_state()
        assert ((root / ".gitmodules").stat().st_ino, missing[0].stat().st_ino) == before
        second._cleanup_sandbox_setup_state()
        assert not (root / ".gitmodules").exists() and not missing[0].exists()
    finally:
        first._cleanup_sandbox_setup_state()
        second._cleanup_sandbox_setup_state()


def test_host_replacement_and_real_empty_metadata_survive_final_release(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    original = root / ".gitconfig"
    original.touch()
    original_inode = original.stat().st_ino
    owner = BubblewrapMountTargets()
    assert owner.acquire(original, kind="directory", protected_metadata=True)
    target = root / ".gitmodules"
    assert owner.acquire(target, kind="directory", protected_metadata=True)
    target.rmdir()
    target.write_text("new real user content\n", encoding="utf-8")
    owner.close()
    assert original.is_file() and original.stat().st_ino == original_inode
    assert target.read_text(encoding="utf-8") == "new real user content\n"


def test_overlapping_process_owner_keeps_the_original_synthetic_inode(tmp_path):
    from backend.sandbox import mount_targets

    target = tmp_path / "synthetic-target"
    ready, release = tmp_path / "child-ready", tmp_path / "release-owner"
    first = BubblewrapMountTargets()
    assert first.acquire(target, kind="directory", protected_metadata=True)
    inode = target.stat().st_ino
    code = '''
import json, pathlib, sys, time
sys.path[:0] = json.loads(sys.argv[1])
from backend.sandbox import mount_targets
mount_targets.MOUNT_TARGET_REGISTRY_ROOT = pathlib.Path(sys.argv[2])
owner = mount_targets.BubblewrapMountTargets()
owner.acquire(pathlib.Path(sys.argv[3]), kind="directory", protected_metadata=True)
pathlib.Path(sys.argv[4]).write_text("ready")
while not pathlib.Path(sys.argv[5]).exists():
    time.sleep(.01)
owner.close()
'''
    process = subprocess.Popen([sys.executable, "-c", code, json.dumps(sys.path), str(mount_targets.MOUNT_TARGET_REGISTRY_ROOT),
        str(target), str(ready), str(release)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists(), process.communicate(timeout=1)
        first.close()
        assert target.is_dir() and target.stat().st_ino == inode
        release.write_text("release", encoding="utf-8")
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, (stdout, stderr)
        assert not target.exists()
    finally:
        release.write_text("release", encoding="utf-8")
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
        first.close()


def test_runner_task_cancellation_returns_cancelled_result_after_real_child_exit(tmp_path):
    runner = SandboxRunner(SandboxPolicy.bypass())
    started = tmp_path / "real-child-started"
    code = "import pathlib, sys, time; pathlib.Path(sys.argv[1]).write_text('started'); time.sleep(60)"

    async def scenario():
        invocation = asyncio.create_task(runner.run("controlled real child", cwd=tmp_path,
            argv=[sys.executable, "-c", code, str(started)]))
        try:
            async def wait_started():
                while not started.exists():
                    if invocation.done():
                        pytest.fail(f"real cancellation child did not start: {invocation.result()}")
                    await asyncio.sleep(.01)
            await asyncio.wait_for(wait_started(), 15)
            child = runner.process
            assert child is not None and child.returncode is None
            invocation.cancel()
            result = await asyncio.wait_for(invocation, 15)
            assert result.cancelled and result.exit_code == -1 and not result.success, result
            assert not result.timed_out and not result.sandbox_unavailable and not result.execution_error, result
            assert not result.cleanup_pending and result.cleanup_reason == "", result
            assert child.returncode is not None
            await asyncio.wait_for(child.wait(), 5)
            assert runner.process is None
            assert await runner.cleanup()
        finally:
            if not invocation.done():
                invocation.cancel()
                await asyncio.gather(invocation, return_exceptions=True)
            assert await runner.cleanup()

    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform != "linux", reason="Requires the real Linux bwrap namespace and POSIX FD transport")
@pytest.mark.parametrize("entry", ["normal", "interactive", "pty"])
@pytest.mark.parametrize("cancel", [False, True], ids=["complete", "cancel"])
def test_real_linux_many_bind_data_masks_keep_targets_through_each_entry(tmp_path, entry, cancel):
    root = tmp_path / "repo"
    root.mkdir()
    original = root / ".mcp.json"
    original.write_text('{"keep":"actual-user-data"}', encoding="utf-8")
    original_identity = (original.stat().st_dev, original.stat().st_ino)
    policy, missing = _policy(root, 12)
    runner = SandboxRunner(policy)
    capability = runner.capability(cwd=root)
    assert capability.available and capability.backend == "bubblewrap", capability.reason
    started, release = root / "child-started", root / "release-child"
    code = '''
import json, pathlib, sys, time
root = pathlib.Path(sys.argv[1])
for index in range(12):
    path = root / f"ordinary-readonly-{index}"
    assert path.is_file() and path.read_bytes() == b""
    try:
        path.write_text("must-not-write")
    except OSError:
        pass
    else:
        raise AssertionError("read-only file was writable")
assert (root / ".gitmodules").is_dir()
assert (root / ".mcp.json").read_text() == '{"keep":"actual-user-data"}'
(root / "child-started").write_text("ready")
while not (root / "release-child").exists():
    time.sleep(.01)
assert (root / ".gitmodules").is_dir()
assert all((root / f"ordinary-readonly-{index}").is_file() for index in range(12))
print("ACTUAL_BIND_DATA_MASKS_OK", flush=True)
'''

    async def scenario():
        process = None
        invocation = None
        try:
            argv = [sys.executable, "-c", code, str(root)]
            if entry == "interactive":
                process = await runner.spawn_interactive(argv, cwd=root)
            else:
                invocation = asyncio.create_task(runner.run(shlex.join(argv), cwd=root, tty=entry == "pty"))

            async def wait_started():
                while not started.exists():
                    if invocation is not None and invocation.done():
                        result = invocation.result()
                        pytest.fail(f"real namespace child did not start: {result}")
                    if process is not None and process.returncode is not None:
                        stdout, stderr = await process.communicate()
                        pytest.fail(f"real interactive child did not start: {stdout!r} {stderr!r}")
                    await asyncio.sleep(.01)
            await asyncio.wait_for(wait_started(), 15)
            targets = runner._bubblewrap_mount_targets
            assert targets is not None and len(targets.preserved_fds) == 12 and max(targets.preserved_fds) >= 10
            assert runner._sandbox_ready_file.exists()
            assert (root / ".gitmodules").is_dir() and all(path.is_file() for path in missing)
            child = runner.process
            assert child is not None and child.returncode is None
            if cancel:
                if invocation is not None:
                    invocation.cancel()
                    result = await asyncio.wait_for(invocation, 15)
                    assert result.cancelled and result.exit_code == -1 and not result.success, result
                    assert not result.timed_out and not result.sandbox_unavailable and not result.execution_error, result
                    assert not result.cleanup_pending and result.cleanup_reason == "", result
                else:
                    assert await runner.terminate(process)
                assert child.returncode is not None
                await asyncio.wait_for(child.wait(), 5)
                assert runner.process is None and runner._bubblewrap_mount_targets is None
            else:
                release.write_text("continue", encoding="utf-8")
                if invocation is not None:
                    result = await asyncio.wait_for(invocation, 15)
                    assert result.exit_code == 0 and "ACTUAL_BIND_DATA_MASKS_OK" in result.stdout, result
                else:
                    stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
                    assert process.returncode == 0 and b"ACTUAL_BIND_DATA_MASKS_OK" in stdout, stderr
            assert await runner.cleanup()
            assert runner.process is None and runner._bubblewrap_mount_targets is None
        finally:
            if invocation is not None and not invocation.done():
                invocation.cancel()
                await asyncio.gather(invocation, return_exceptions=True)
            assert await runner.cleanup()

    asyncio.run(scenario())
    assert original.read_text(encoding="utf-8") == '{"keep":"actual-user-data"}'
    assert (original.stat().st_dev, original.stat().st_ino) == original_identity
    assert not (root / ".gitmodules").exists() and all(not path.exists() for path in missing)
    print("BWRAP_TARGET_LIFETIME_ORACLE=" + json.dumps({"entry": entry, "cancel": cancel, "backend": capability.backend,
        "bind_data_masks": 12, "fd_at_least_10": True, "real_metadata_preserved": True, "targets_released_after_child": True}))


@pytest.mark.skipif(sys.platform != "linux", reason="Requires two real Linux bwrap children")
def test_real_linux_overlapping_children_keep_private_masks_after_first_cleanup(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    policy, missing = _policy(root, 1)
    first, second = SandboxRunner(policy), SandboxRunner(policy)
    capability = first.capability(cwd=root)
    assert capability.available and capability.backend == "bubblewrap", capability.reason
    code = '''
import pathlib, sys, time
root, ready, release = map(pathlib.Path, sys.argv[1:])
ready.write_text("ready")
while not release.exists():
    time.sleep(.01)
assert (root / ".gitmodules").is_dir()
assert (root / "ordinary-readonly-0").read_bytes() == b""
print("OVERLAPPING_PRIVATE_MASKS_OK", flush=True)
'''

    async def scenario():
        children = []
        try:
            for index, runner in enumerate((first, second)):
                ready, release = root / f"ready-{index}", root / f"release-{index}"
                process = await runner.spawn_interactive([sys.executable, "-c", code, str(root), str(ready), str(release)], cwd=root)
                children.append((runner, process, release))
                async def wait_started():
                    while not ready.exists():
                        if process.returncode is not None:
                            pytest.fail(f"overlapping child exited: {await process.communicate()}")
                        await asyncio.sleep(.01)
                await asyncio.wait_for(wait_started(), 15)
            before = (root / ".gitmodules").stat().st_ino, missing[0].stat().st_ino
            children[0][2].write_text("complete", encoding="utf-8")
            stdout, stderr = await asyncio.wait_for(children[0][1].communicate(), 15)
            assert children[0][1].returncode == 0 and b"OVERLAPPING_PRIVATE_MASKS_OK" in stdout, stderr
            assert await first.cleanup()
            assert ((root / ".gitmodules").stat().st_ino, missing[0].stat().st_ino) == before
            children[1][2].write_text("complete", encoding="utf-8")
            stdout, stderr = await asyncio.wait_for(children[1][1].communicate(), 15)
            assert children[1][1].returncode == 0 and b"OVERLAPPING_PRIVATE_MASKS_OK" in stdout, stderr
            assert await second.cleanup()
        finally:
            for runner, process, release in children:
                release.write_text("cleanup", encoding="utf-8")
                assert await runner.terminate(process)

    asyncio.run(scenario())
    assert not (root / ".gitmodules").exists() and not missing[0].exists()
    print("BWRAP_OVERLAP_ORACLE=" + json.dumps({"backend": capability.backend, "actual_children": 2,
        "second_private_masks_after_first_cleanup": True, "targets_released_after_last_child": True}))


@pytest.mark.skipif(sys.platform != "linux", reason="Requires the real Linux namespace deny mask")
def test_real_linux_registry_bind_does_not_reopen_its_denied_parent(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr("backend.sandbox.mount_targets.MOUNT_TARGET_REGISTRY_ROOT", tmp_path / "denied-parent" / "registry")
    targets = BubblewrapMountTargets()
    targets.registry_root.mkdir(parents=True)
    (targets.registry_root / "host-marker").write_text("must-remain-hidden", encoding="utf-8")
    runner = SandboxRunner(_registry_parent_denied_policy(root, targets))
    capability = runner.capability(cwd=root)
    assert capability.available and capability.backend == "bubblewrap", capability.reason
    code = '''
import pathlib, sys
parent, registry = map(pathlib.Path, sys.argv[1:])
for path in (parent, registry):
    try:
        list(path.iterdir())
    except OSError:
        pass
    else:
        raise AssertionError("registry protection reopened a denied parent")
print("REGISTRY_DENIED_PARENT_HELD", flush=True)
'''
    result = asyncio.run(runner.run(shlex.join([sys.executable, "-c", code,
        str(targets.registry_root.parent), str(targets.registry_root)]), cwd=root))
    assert result.exit_code == 0 and "REGISTRY_DENIED_PARENT_HELD" in result.stdout, result


@pytest.mark.skipif(sys.platform != "linux", reason="Requires the real Linux zero-permission generic file mask")
def test_real_linux_generic_missing_deny_file_is_unreadable_and_hides_host_content(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    denied = root / "ordinary-denied-file"
    started, release = root / "deny-child-started", root / "release-deny-child"
    policy = readable_host_git_policy(SandboxPolicy(workspace_root=root, writable_roots=(root,), denied_roots=(denied,)))
    runner = SandboxRunner(policy)
    capability = runner.capability(cwd=root)
    assert capability.available and capability.backend == "bubblewrap", capability.reason
    code = '''
import errno, pathlib, stat, sys, time
denied, started, release = map(pathlib.Path, sys.argv[1:])
def check_denied():
    assert stat.S_ISREG(denied.stat().st_mode)
    assert stat.S_IMODE(denied.stat().st_mode) == 0
    try:
        denied.read_bytes()
    except PermissionError:
        pass
    else:
        raise AssertionError("DENY was exposed as a readable empty file")
    try:
        denied.write_bytes(b"must-not-write")
    except OSError as error:
        assert error.errno in (errno.EACCES, errno.EROFS)
    else:
        raise AssertionError("DENY file was writable")
check_denied()
started.write_text("ready")
while not release.exists():
    time.sleep(.01)
check_denied()
print("GENERIC_FILE_DENY_CONTENT_AND_PERMISSIONS_HELD", flush=True)
'''

    async def scenario():
        process = None
        try:
            process = await runner.spawn_interactive([sys.executable, "-c", code, str(denied), str(started), str(release)], cwd=root)
            async def wait_started():
                while not started.exists():
                    if process.returncode is not None:
                        pytest.fail(f"generic DENY child exited: {await process.communicate()}")
                    await asyncio.sleep(.01)
            await asyncio.wait_for(wait_started(), 15)
            assert runner._bubblewrap_mount_targets.masks[denied] == "file" and denied.is_file()
            identity = denied.stat().st_dev, denied.stat().st_ino
            # A real host mutation cannot replace the child's private bind-data
            # contents, and cleanup must preserve that newly real user content.
            denied.write_text("HOST_SECRET_DONT_PROJECT", encoding="utf-8")
            release.write_text("continue", encoding="utf-8")
            stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
            assert process.returncode == 0 and b"GENERIC_FILE_DENY_CONTENT_AND_PERMISSIONS_HELD" in stdout, stderr
            assert await runner.cleanup()
            assert denied.read_text(encoding="utf-8") == "HOST_SECRET_DONT_PROJECT"
            assert (denied.stat().st_dev, denied.stat().st_ino) == identity
        finally:
            assert await runner.cleanup()

    asyncio.run(scenario())
    print("BWRAP_GENERIC_DENY_ORACLE=" + json.dumps({"backend": capability.backend, "kind": "generic_file",
        "mode": "000", "read_permission_denied": True, "write_denied": True, "host_content_hidden": True,
        "changed_host_inode_preserved": True}))
