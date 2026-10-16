from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess

import psutil
import pytest

from backend.environments import EnvironmentManager, ExecutorClosed, ExecutorRpcError
from backend.sandbox.runner import SandboxRunner
from backend.workspace.executor_local_fs import LocalExecutorFileSystem, _helper_policy


def sandbox_for(root: Path, *, denied: Path | None = None) -> dict:
    entries = [{"path": {"type": "special", "value": {"kind": "root"}}, "access": "read"},
               {"path": {"type": "path", "path": root.as_uri()}, "access": "write"}]
    if denied is not None:
        entries.append({"path": {"type": "path", "path": denied.as_uri()}, "access": "deny"})
    return {"permissions": {"type": "managed", "file_system": {"type": "restricted", "entries": entries}, "network": "restricted"},
            "cwd": root.as_uri(), "workspaceRoots": [root.as_uri()], "windowsSandboxLevel": "elevated", "useLegacyLandlock": False}


def require_actual_sandbox(sandbox: dict):
    policy = _helper_policy(sandbox)
    capability = SandboxRunner(policy).capability(cwd=policy.policy_cwd)
    if not capability.available or not capability.filesystem_isolated:
        pytest.skip(f"actual platform filesystem sandbox unavailable: {capability.reason}")
    return capability


@pytest.mark.asyncio
async def test_local_filesystem_direct_full_crud_binary_copy_and_source_shapes(tmp_path):
    fs = LocalExecutorFileSystem()
    folder = tmp_path / "中文目录"
    await fs.create_directory(folder.as_uri(), sandbox=None)
    path = folder / "binary.bin"
    contents = b"\x00\xff\n" * 19
    await fs.write_file(path.as_uri(), contents, sandbox=None)
    assert await fs.read_file(path.as_uri(), sandbox=None) == contents
    value = await fs.get_metadata(path.as_uri(), sandbox=None)
    assert value["isFile"] and not value["isDirectory"] and not value["isSymlink"] and value["size"] == len(contents)
    assert isinstance(value["createdAtMs"], int) and isinstance(value["modifiedAtMs"], int)
    assert await fs.canonicalize(path.as_uri(), sandbox=None) == path.resolve().as_uri()
    assert await fs.read_directory(folder.as_uri(), sandbox=None) == [{"fileName": "binary.bin", "isDirectory": False, "isFile": True}]
    destination = tmp_path / "copied"
    await fs.copy(folder.as_uri(), destination.as_uri(), sandbox=None, recursive=True)
    assert (destination / path.name).read_bytes() == contents
    with pytest.raises(OSError, match="descendant"):
        await fs.copy(folder.as_uri(), (folder / "child").as_uri(), sandbox=None, recursive=True)
    await fs.remove(destination.as_uri(), sandbox=None, recursive=True)
    assert not destination.exists()
    await fs.remove(destination.as_uri(), sandbox=None, force=True)
    await fs.close()
    with pytest.raises(ExecutorClosed):
        await fs.read_file(path.as_uri(), sandbox=None)


@pytest.mark.asyncio
async def test_local_stream_offset_binary_chunks_terminal_close_and_owner_close(tmp_path):
    fs = LocalExecutorFileSystem()
    path = tmp_path / "stream.bin"
    first = b"a" * (1024 * 1024)
    writer = await fs.open(path.as_uri(), sandbox=None, mode="replace")
    await writer.write_block(0, first)
    await writer.write_block(len(first), b"\x00\xff")
    await writer.write_block(2, b"replacement")
    await writer.aclose()
    assert (await writer.aclose())["closed"]
    expected = b"aa" + b"replacement" + first[13:] + b"\x00\xff"
    reader = await fs.open(path.as_uri(), sandbox=None)
    assert b"".join([chunk async for chunk in reader.chunks()]) == expected
    assert reader.closed and not fs.files
    pending = await fs.open(path.as_uri(), sandbox=None)
    await fs.close()
    assert pending.closed and not fs.files
    with pytest.raises(ExecutorClosed):
        await pending.read_block(0)


@pytest.mark.asyncio
async def test_local_stream_source_handle_capacity_and_signed_write_limit(tmp_path):
    path = tmp_path / "bounded-handles"
    path.write_bytes(b"preserve")
    fs = LocalExecutorFileSystem()
    try:
        handles = [await fs.open(path.as_uri(), sandbox=None) for _ in range(128)]
        with pytest.raises(OSError, match="128"):
            await fs.open(path.as_uri(), sandbox=None)
        await handles[0].aclose()
        replacement = await fs.open(path.as_uri(), sandbox=None)
        assert await replacement.read_block(0, 8) == (b"preserve", False)
        await fs.close()
        writer = LocalExecutorFileSystem()
        try:
            target = await writer.open((tmp_path / "range").as_uri(), sandbox=None, mode="replace")
            with pytest.raises(OSError, match="signed 64-bit"):
                await target.write_block((1 << 63) - 1, b"x")
            await target.write_block(0, b"valid-after-invalid-range")
            assert (tmp_path / "range").read_bytes() == b"valid-after-invalid-range"
        finally:
            await writer.close()
    finally:
        await fs.close()


@pytest.mark.asyncio
async def test_local_nofollow_native_regular_operations(tmp_path):
    fs = LocalExecutorFileSystem()
    folder = tmp_path / "one" / "two"
    await fs.create_directory(folder.as_uri(), sandbox=None, recursive=True, follow_symlinks=False)
    path = folder / "file-🙂.bin"
    await fs.write_file(path.as_uri(), b"first-long", sandbox=None, follow_symlinks=False)
    await fs.write_file(path.as_uri(), b"new", sandbox=None, follow_symlinks=False)
    assert await fs.read_file(path.as_uri(), sandbox=None, follow_symlinks=False) == b"new"
    assert (await fs.get_metadata(folder.as_uri(), sandbox=None, follow_symlinks=False))["isDirectory"]
    assert (await fs.get_metadata(path.as_uri(), sandbox=None, follow_symlinks=False))["isFile"]
    with pytest.raises(OSError, match="recursive no-follow"):
        await fs.remove(folder.as_uri(), sandbox=None, recursive=True, follow_symlinks=False)
    await fs.remove(path.as_uri(), sandbox=None, follow_symlinks=False)
    await fs.remove(folder.as_uri(), sandbox=None, follow_symlinks=False)
    assert not folder.exists()
    await fs.close()


def make_symlink(target: Path, link: Path, *, directory: bool = False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as error:
        if os.name == "nt" and error.winerror == 1314:
            pytest.skip("Windows account cannot create a symlink; no symlink result claimed")
        raise


@pytest.mark.asyncio
async def test_local_nofollow_refuses_final_and_intermediate_links_without_touching_target(tmp_path):
    fs = LocalExecutorFileSystem()
    actual = tmp_path / "actual"
    actual.mkdir()
    target = actual / "keep.txt"
    target.write_bytes(b"must-remain")
    file_link, directory_link = tmp_path / "file-link", tmp_path / "directory-link"
    make_symlink(target, file_link)
    make_symlink(actual, directory_link, directory=True)
    for path in (file_link, directory_link / target.name):
        with pytest.raises(OSError):
            await fs.read_file(path.as_uri(), sandbox=None, follow_symlinks=False)
        with pytest.raises(OSError):
            await fs.write_file(path.as_uri(), b"must-not-write", sandbox=None, follow_symlinks=False)
        with pytest.raises(OSError):
            await fs.get_metadata(path.as_uri(), sandbox=None, follow_symlinks=False)
        with pytest.raises(OSError):
            await fs.remove(path.as_uri(), sandbox=None, follow_symlinks=False)
    with pytest.raises(OSError):
        await fs.create_directory((directory_link / "must-not-create").as_uri(), sandbox=None, follow_symlinks=False)
    assert target.read_bytes() == b"must-remain" and not (actual / "must-not-create").exists()
    assert await fs.read_file(file_link.as_uri(), sandbox=None) == b"must-remain"
    assert (await fs.get_metadata(file_link.as_uri(), sandbox=None))["isSymlink"]
    await fs.close()


@pytest.mark.asyncio
async def test_local_walk_source_order_hidden_prune_symlinks_cycles_and_limits(tmp_path):
    fs = LocalExecutorFileSystem()
    root = tmp_path / "walk"
    root.mkdir()
    (root / "a.txt").write_text("a")
    hidden, visible = root / ".hidden", root / "visible"
    hidden.mkdir()
    visible.mkdir()
    (hidden / "omit.txt").write_text("omit")
    (visible / "b.txt").write_text("b")
    options = {"maxDepth": 64, "maxDirectories": 100, "maxEntries": 100, "followDirectorySymlinks": True, "pruneHiddenDirectories": True}
    result = await fs.walk(root.as_uri(), options, sandbox=None)
    assert not result["truncated"] and result["errors"] == []
    assert [item["path"] for item in result["entries"]] == [
        hidden.as_uri(), (root / "a.txt").as_uri(), visible.as_uri(),
        (visible / "b.txt").as_uri(),
    ]
    result = await fs.walk(root.as_uri(), {**options, "maxEntries": 1}, sandbox=None)
    assert result["truncated"] and len(result["entries"]) == 1
    with pytest.raises(OSError, match="maximums"):
        await fs.walk(root.as_uri(), {**options, "maxDepth": 65}, sandbox=None)
    await fs.close()


@pytest.mark.asyncio
async def test_local_walk_skips_file_and_broken_links_and_does_not_revisit_directory_cycle(tmp_path):
    fs = LocalExecutorFileSystem()
    root = tmp_path / "walk"
    root.mkdir()
    (root / "a.txt").write_text("a")
    make_symlink(root, root / "cycle", directory=True)
    make_symlink(root / "a.txt", root / "file-link")
    make_symlink(root / "missing", root / "broken")
    options = {"maxDepth": 64, "maxDirectories": 100, "maxEntries": 100, "followDirectorySymlinks": True}
    result = await fs.walk(root.as_uri(), options, sandbox=None)
    assert result == {"entries": [{"path": (root / "a.txt").as_uri(), "kind": "file"},
                                  {"path": (root / "cycle").as_uri(), "kind": "directory"}], "errors": [], "truncated": False}
    result = await fs.walk(root.as_uri(), {**options, "followDirectorySymlinks": False}, sandbox=None)
    assert result["entries"] == [{"path": (root / "a.txt").as_uri(), "kind": "file"}]
    await fs.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows NtCreateFile reparse-point admission")
@pytest.mark.asyncio
async def test_local_nofollow_kernel_refuses_directory_junction_without_symlink_privilege(tmp_path):
    actual, junction = tmp_path / "actual", tmp_path / "junction"
    actual.mkdir()
    target = actual / "preserve"
    target.write_bytes(b"must-remain")
    subprocess.run([os.environ["COMSPEC"], "/d", "/c", "mklink", "/J", str(junction), str(actual)],
                   check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    fs = LocalExecutorFileSystem()
    with pytest.raises(OSError, match="reparse point"):
        await fs.read_file((junction / target.name).as_uri(), sandbox=None, follow_symlinks=False)
    with pytest.raises(OSError, match="reparse point"):
        await fs.write_file((junction / target.name).as_uri(), b"must-not-change", sandbox=None, follow_symlinks=False)
    with pytest.raises(OSError, match="reparse point"):
        await fs.get_metadata(junction.as_uri(), sandbox=None, follow_symlinks=False)
    with pytest.raises(OSError, match="reparse point"):
        await fs.create_directory((junction / "must-not-exist").as_uri(), sandbox=None, follow_symlinks=False)
    with pytest.raises(OSError, match="reparse point"):
        await fs.remove((junction / target.name).as_uri(), sandbox=None, follow_symlinks=False)
    assert target.read_bytes() == b"must-remain" and not (actual / "must-not-exist").exists()
    with pytest.raises(OSError):
        await fs.remove(junction.as_uri(), sandbox=None, recursive=True)
    assert junction.exists() and target.read_bytes() == b"must-remain"
    await fs.close()


@pytest.mark.asyncio
async def test_local_cancelled_open_closes_real_descriptor_before_return(tmp_path, monkeypatch):
    path = tmp_path / "cancel-open"
    path.write_bytes(b"preserve")
    fs = LocalExecutorFileSystem()
    arrived = asyncio.Event()
    descriptors = []
    original = fs._direct

    async def delayed_direct(request, handles=None):
        result = await original(request, handles)
        if request["operation"] == "open":
            descriptors.extend(descriptor for descriptor, _ in handles.values())
            arrived.set()
            await asyncio.Event().wait()
        return result

    monkeypatch.setattr(fs, "_direct", delayed_direct)
    opening = asyncio.create_task(fs.open(path.as_uri(), sandbox=None))
    await arrived.wait()
    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    assert not fs.files and path.read_bytes() == b"preserve"
    await fs.close()


@pytest.mark.asyncio
async def test_local_owner_close_cancels_inflight_open_and_keeps_original_descriptor_owned(tmp_path, monkeypatch):
    path = tmp_path / "close-during-open"
    path.write_bytes(b"preserve")
    fs = LocalExecutorFileSystem()
    arrived = asyncio.Event()
    descriptors = []
    original = fs._direct

    async def delayed_direct(request, handles=None):
        result = await original(request, handles)
        if request["operation"] == "open":
            descriptors.extend(descriptor for descriptor, _ in handles.values())
            arrived.set()
            await asyncio.Event().wait()
        return result

    monkeypatch.setattr(fs, "_direct", delayed_direct)
    opening = asyncio.create_task(fs.open(path.as_uri(), sandbox=None))
    await arrived.wait()
    await fs.close()
    with pytest.raises(asyncio.CancelledError):
        await opening
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    assert not fs.files and not fs.operations and path.read_bytes() == b"preserve"


@pytest.mark.skipif(os.name == "nt", reason="POSIX native byte paths")
@pytest.mark.asyncio
async def test_local_native_non_utf8_bytes_keep_source_uri_through_read_canonicalize_and_walk(tmp_path):
    from backend.workspace.executor_paths import ExecutorPath

    native_root = os.fsencode(tmp_path) + b"/raw-\xff"
    os.mkdir(native_root)
    native = native_root + b"/content"
    descriptor = os.open(native, os.O_CREAT | os.O_WRONLY, 0o600)
    os.write(descriptor, b"raw-content")
    os.close(descriptor)
    uri = ExecutorPath.from_native(native, convention="posix").uri
    fs = LocalExecutorFileSystem()
    assert await fs.read_file(uri, sandbox=None) == b"raw-content"
    assert await fs.canonicalize(uri, sandbox=None) == uri
    root_uri = ExecutorPath.from_native(native_root, convention="posix").uri
    result = await fs.walk(root_uri, {"maxDepth": 1, "maxDirectories": 10, "maxEntries": 10,
        "followDirectorySymlinks": False}, sandbox=None)
    assert result == {"entries": [{"path": uri, "kind": "file"}], "errors": [], "truncated": False}
    await fs.close()


@pytest.mark.asyncio
async def test_local_read_ceiling_modes_block_bounds_and_required_sandbox(tmp_path):
    fs = LocalExecutorFileSystem()
    path = tmp_path / "too-large"
    with path.open("wb") as stream:
        if os.name == "nt":
            import ctypes
            import msvcrt

            control = ctypes.WinDLL("kernel32", use_last_error=True).DeviceIoControl
            control.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint,
                                ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint), ctypes.c_void_p]
            returned = ctypes.c_uint()
            assert control(msvcrt.get_osfhandle(stream.fileno()), 0x900C4, None, 0, None, 0, ctypes.byref(returned), None)
        stream.truncate(512 * 1024 * 1024 + 1)
    with pytest.raises(OSError, match="too large"):
        await fs.read_file(path.as_uri(), sandbox=None)
    with pytest.raises(TypeError):
        await fs.read_file(path.as_uri())
    with pytest.raises(OSError, match="mode"):
        await fs.open(path.as_uri(), sandbox=None, mode="invented")
    assert not fs.files
    path.write_bytes(b"short")
    reader = await fs.open(path.as_uri(), sandbox=None)
    with pytest.raises(OSError):
        await reader.read_block(0, 0)
    with pytest.raises(OSError):
        await reader.write_block(0, b"cannot-write-read-mode")
    with pytest.raises(OSError):
        await reader.read_block(-1)
    await fs.close()


@pytest.mark.asyncio
async def test_local_foreign_uri_never_touches_current_host(tmp_path):
    fs = LocalExecutorFileSystem()
    foreign = "file:///etc/passwd" if os.name == "nt" else "file:///C:/Windows/win.ini"
    with pytest.raises((OSError, ValueError)):
        await fs.read_file(foreign, sandbox=None)
    await fs.close()


@pytest.mark.asyncio
async def test_actual_platform_helper_enforces_original_read_write_scope_and_stream_owner(tmp_path):
    workspace, outside = tmp_path / "workspace", tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    secret = outside / "secret"
    secret.write_bytes(b"controller-can-read-but-helper-cannot")
    sandbox = sandbox_for(workspace, denied=outside)
    capability = require_actual_sandbox(sandbox)
    fs = LocalExecutorFileSystem()
    path = workspace / "owned.bin"
    try:
        await fs.write_file(path.as_uri(), b"owned", sandbox=sandbox)
        assert await fs.read_file(path.as_uri(), sandbox=sandbox) == b"owned"
        with pytest.raises(OSError):
            await fs.read_file(secret.as_uri(), sandbox=sandbox)
        writer = await fs.open(path.as_uri(), sandbox=sandbox, mode="replace")
        assert writer.helper is not None
        assert writer.helper.runner.capability().backend == capability.backend
        sandbox["permissions"]["file_system"]["entries"].clear()
        await writer.write_block(0, b"original-scope")
        await writer.aclose()
        assert path.read_bytes() == b"original-scope"
        assert secret.read_bytes() == b"controller-can-read-but-helper-cannot"
        assert not fs.helpers and not fs.files
    finally:
        await fs.close()


@pytest.mark.asyncio
async def test_actual_platform_cancelled_block_closes_original_helper_and_file(tmp_path, monkeypatch):
    sandbox = sandbox_for(tmp_path, denied=tmp_path / "denied")
    require_actual_sandbox(sandbox)
    path = tmp_path / "stream"
    path.write_bytes(b"x" * (1024 * 1024))
    fs = LocalExecutorFileSystem()
    handle = await fs.open(path.as_uri(), sandbox=sandbox)
    child = handle.helper.process
    arrived = asyncio.Event()
    original = child.stdout.readexactly

    async def blocked_read(size):
        arrived.set()
        await asyncio.Event().wait()
        return await original(size)

    monkeypatch.setattr(child.stdout, "readexactly", blocked_read)
    request = asyncio.create_task(handle.read_block(0))
    await arrived.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert child.returncode is not None and not psutil.pid_exists(child.pid)
    await handle.aclose()
    await fs.close()
    assert not fs.helpers and not fs.files


@pytest.mark.asyncio
async def test_local_native_results_match_actual_installed_source_exec_server(tmp_path):
    source = os.environ.get("MINICODE_SOURCE_EXECUTOR")
    if source is None:
        pytest.skip("MINICODE_SOURCE_EXECUTOR is not configured; actual Source interoperability not claimed")
    home = tmp_path / "owned-source-home"
    home.mkdir()
    manager = EnvironmentManager(tmp_path, environ={"CODEX_EXEC_SERVER_URL": "none"})
    reference = manager.upsert({"id": "reference", "program": source,
        "args": ["exec-server", "--listen", "stdio", "--concurrent-requests", "4"], "env": {"CODEX_HOME": str(home)}})
    local = LocalExecutorFileSystem()
    folder = tmp_path / "files"
    folder.mkdir()
    target = folder / "same-🙂.bin"
    target.write_bytes(b"\x00\xffsame-source")
    try:
        assert await local.read_file(target.as_uri(), sandbox=None) == await reference.filesystem.read_file(target.as_uri(), sandbox=None)
        assert await local.get_metadata(target.as_uri(), sandbox=None) == await reference.filesystem.get_metadata(target.as_uri(), sandbox=None)
        assert await local.canonicalize(target.as_uri(), sandbox=None) == await reference.filesystem.canonicalize(target.as_uri(), sandbox=None)
        options = {"maxDepth": 3, "maxDirectories": 10, "maxEntries": 10, "followDirectorySymlinks": False}
        assert await local.walk(folder.as_uri(), options, sandbox=None) == await reference.filesystem.walk(folder.as_uri(), options, sandbox=None)
        if os.name == "nt":
            junction = tmp_path / "reference-junction"
            subprocess.run([os.environ["COMSPEC"], "/d", "/c", "mklink", "/J", str(junction), str(folder)],
                           check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            assert await local.get_metadata(junction.as_uri(), sandbox=None) == await reference.filesystem.get_metadata(junction.as_uri(), sandbox=None)
            for follow_links in (False, True):
                walk_options = {**options, "followDirectorySymlinks": follow_links}
                assert await local.walk(junction.as_uri(), walk_options, sandbox=None) == await reference.filesystem.walk(junction.as_uri(), walk_options, sandbox=None)
            with pytest.raises(ExecutorRpcError, match="reparse point"):
                await reference.filesystem.read_file((junction / target.name).as_uri(), sandbox=None, follow_symlinks=False)
            with pytest.raises(OSError, match="reparse point"):
                await local.read_file((junction / target.name).as_uri(), sandbox=None, follow_symlinks=False)
            with pytest.raises(ExecutorRpcError):
                await reference.filesystem.remove(junction.as_uri(), sandbox=None, recursive=True)
            with pytest.raises(OSError):
                await local.remove(junction.as_uri(), sandbox=None, recursive=True)
            assert target.read_bytes() == b"\x00\xffsame-source" and junction.exists()
        child = reference.client.connection.transport.child
    finally:
        await local.close()
        await manager.close()
    assert child.returncode == 0 and not reference.client.transport_receipts[-1].forced
