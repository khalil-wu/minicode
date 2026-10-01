from __future__ import annotations

import asyncio
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.services import conversation_worktree_handoff_service as handoff
from backend.services import health_service, workspace_api_service, workspace_service
from backend.sandbox.runner import SandboxUnavailableError
from backend.tools import git_support


def _result(argv, stdout="", *, code=0, stderr=""):
    return subprocess.CompletedProcess(argv, code, stdout.encode("utf-8"), stderr.encode("utf-8"))


def test_sync_snapshot_from_async_host_waits_for_canonical_owner(monkeypatch, tmp_path):
    released = threading.Event()
    entered = threading.Event()
    calls = []

    async def canonical(argv, *, root, cwd, sandbox_policy, timeout):
        assert cwd == root and sandbox_policy.workspace_root == root
        calls.append((argv, root, timeout, threading.get_ident()))
        entered.set()
        while not released.is_set():
            await asyncio.sleep(0.01)
        return _result(argv, "完成\n")

    monkeypatch.setattr(git_support, "_run_git", canonical)

    async def oracle():
        host_thread = threading.get_ident()
        pending = asyncio.create_task(asyncio.to_thread(workspace_service.run_readonly_git, tmp_path, "status"))
        await asyncio.to_thread(entered.wait)
        assert not pending.done()
        released.set()
        result = await pending
        assert result.stdout == "完成\n"
        assert calls == [(["git", "status"], tmp_path, 5, calls[0][3])]
        assert calls[0][3] != host_thread
        # The unchanged synchronous API is also callable inside a live host loop.
        assert workspace_service.run_readonly_git(tmp_path, "branch").stdout == "完成\n"

    asyncio.run(oracle())


@pytest.mark.parametrize("unborn", [False, True])
def test_workspace_status_and_diff_share_canonical_boundary(monkeypatch, tmp_path, unborn):
    calls = []

    async def canonical(argv, *, root, cwd, sandbox_policy, timeout):
        assert cwd == root and sandbox_policy.workspace_root == root
        assert root == tmp_path
        assert timeout == 10
        args = argv[2:]
        assert argv[:2] == ["git", "--literal-pathspecs"]
        calls.append(tuple(args))
        if args == ["rev-parse", "--show-prefix"]:
            return _result(argv)
        if args[:2] == ["status", "--porcelain=v1"]:
            return _result(argv, "## main\0 M tracked.txt\0?? new.txt\0")
        if args == ["rev-parse", "--show-toplevel"]:
            return _result(argv, "/workspace\n")
        if args == ["rev-parse", "--verify", "--quiet", "HEAD"]:
            return _result(argv, "" if unborn else "head\n", code=1 if unborn else 0)
        if args == ["hash-object", "-t", "tree", "--stdin"]:
            return _result(argv, "empty-tree\n")
        if args[0] == "ls-files":
            return _result(argv, "new.txt\0")
        if args[0] == "diff":
            assert "--no-textconv" in args and "--no-ext-diff" in args
            return _result(argv, "new patch\n" if "--no-index" in args else "tracked patch\n", code=1 if "--no-index" in args else 0)
        raise AssertionError(args)

    monkeypatch.setattr(git_support, "_run_git", canonical)
    status = workspace_api_service.workspace_git_status_payload(tmp_path)
    assert status == {"branch": "main", "modified": ["tracked.txt"], "staged": [], "untracked": ["new.txt"]}
    assert workspace_api_service.workspace_git_diff_payload(tmp_path, "") == {"diff": "tracked patch\nnew patch\n"}
    assert (("hash-object", "-t", "tree", "--stdin") in calls) == unborn


def test_doctor_services_and_handoff_read_snapshots_use_request_root(monkeypatch, tmp_path):
    calls = []
    main = tmp_path / "main"
    main.mkdir()

    async def canonical(argv, *, root, cwd, sandbox_policy, timeout):
        assert cwd == root and sandbox_policy.workspace_root == root
        assert root == main
        calls.append(tuple(argv[1:]))
        if argv[1] == "branch":
            return _result(argv, "main\n")
        if argv[1] == "status":
            return _result(argv)
        if argv[1] == "worktree":
            return _result(argv, "worktree /workspace\nHEAD abc\nbranch refs/heads/main\n")
        if argv[1] == "rev-parse":
            return _result(argv, "abc\n")
        if argv[1] == "show-ref":
            return _result(argv, code=1)
        if argv[1] == "ls-files":
            return _result(argv)
        raise AssertionError(argv)

    monkeypatch.setattr(git_support, "_run_git", canonical)
    monkeypatch.setattr(workspace_service, "readonly_git_host_path", lambda root, value, **kwargs: root if value == "/workspace" else Path(value))
    assert workspace_service.git_branch_for(main) == "main"
    assert workspace_service.main_worktree_root(main) == main
    assert not workspace_service.worktree_has_local_changes(main)
    assert health_service.build_git_doctor_payload(main) == {"available": True, "branch": "main", "changed": 0, "clean": True, "error": ""}
    conversation = SimpleNamespace(id="target-b", git_isolated=False, workspace_root=str(main), worktree_path="", git_branch="")
    preflight = handoff.build_handoff_preflight(
        conversation, target="worktree", conversation_repo=SimpleNamespace(list_conversations=lambda: []),
        main_worktree_root=workspace_service.main_worktree_root, has_running_turn=False,
    )
    assert preflight["allowed"] is True
    assert preflight["source"]["path"] == str(main)
    assert preflight["conversation_id"] == "target-b"
    assert ("show-ref", "--verify", "refs/heads/minicode/target-b") in calls


def test_handoff_mutations_keep_explicit_control_plane(monkeypatch, tmp_path):
    writes = []
    reads = []
    marker = ""

    def control(root, *args):
        nonlocal marker
        assert root == tmp_path
        writes.append(args)
        if args[:2] == ("stash", "push"):
            marker = args[-1]
            return True, "Saved working directory"
        return True, ""

    async def canonical(argv, *, root, cwd, sandbox_policy, timeout):
        assert cwd == root and sandbox_policy.workspace_root == root
        reads.append(argv)
        assert root == tmp_path
        assert argv[1:] == ["stash", "list", "--format=%H%x00%gs"]
        return _result(argv, f"{'a' * 40}\0On main: {marker}\n")

    monkeypatch.setattr(handoff, "_git", control)
    monkeypatch.setattr(git_support, "_run_git", canonical)
    assert handoff.stash_workspace_changes(tmp_path, label="handoff") == (True, "a" * 40)
    assert handoff.restore_workspace_stash(tmp_path, "a" * 40)[0]
    assert handoff.switch_main_checkout(tmp_path, "next")[0]
    assert handoff.restore_main_checkout(tmp_path, branch="main")[0]
    assert handoff.delete_local_branch(tmp_path, "next")[0]
    assert len(reads) == 1
    assert [args[:2] for args in writes] == [("stash", "push"), ("stash", "apply"), ("switch", "next"), ("switch", "main"), ("branch", "-D")]


def test_namespace_unavailable_is_not_an_empty_success(monkeypatch, tmp_path):
    async def canonical(argv, *, root, cwd, sandbox_policy, timeout):
        raise SandboxUnavailableError("actual namespace unavailable")

    monkeypatch.setattr(git_support, "_run_git", canonical)
    assert workspace_api_service.workspace_git_status_payload(tmp_path)["error"] == "actual namespace unavailable"
    assert workspace_api_service.workspace_git_diff_payload(tmp_path, "")["error"] == "actual namespace unavailable"
    doctor = health_service.build_git_doctor_payload(tmp_path)
    assert doctor["available"] is False and doctor["clean"] is False
    assert handoff._status(tmp_path) == "<status unavailable>"


def test_repository_path_outputs_use_boundary_mapping(monkeypatch, tmp_path):
    async def canonical(argv, *, root, cwd, sandbox_policy, timeout):
        return _result(argv, "/workspace/.git\n")

    monkeypatch.setattr(git_support, "_run_git", canonical)
    seen = []

    def host_path(root, value, **kwargs):
        seen.append((root, value))
        return root / ".git"

    monkeypatch.setattr(workspace_api_service, "readonly_git_host_path", host_path)
    assert workspace_api_service.resolve_git_common_dir(tmp_path) == tmp_path / ".git"
    assert seen == [(tmp_path, "/workspace/.git")]


def test_readonly_metadata_grant_comes_from_trusted_original_registry(monkeypatch, tmp_path):
    from backend.workspace import trust
    from backend.sandbox.policy import FileSystemAccessMode

    original = tmp_path / "original"
    checkout = original / ".minicode" / "worktrees" / "target-b"
    checkout.mkdir(parents=True)
    metadata = original / ".git"
    registry = metadata / "worktrees" / "registered-b"
    registry.mkdir(parents=True)
    external = tmp_path / "unrelated" / ".git"
    external.mkdir(parents=True)
    (checkout / ".git").write_text(f"gitdir: {external.as_posix()}\n", encoding="utf-8")
    backlink = registry / "gitdir"
    backlink.write_text(str(checkout / ".git"), encoding="utf-8")

    monkeypatch.setattr(trust, "is_workspace_trusted", lambda root: False)
    assert workspace_service.readonly_git_policy(checkout).readable_roots == (checkout,)
    monkeypatch.setattr(trust, "is_workspace_trusted", lambda root: root == original)
    backlink.write_text(str(original / ".minicode" / "worktrees" / "other" / ".git"), encoding="utf-8")
    assert workspace_service.readonly_git_policy(checkout).readable_roots == (checkout,)
    backlink.write_text(str(checkout / ".git"), encoding="utf-8")
    policy = workspace_service.readonly_git_policy(checkout)
    assert policy.readable_roots == (checkout, metadata)
    assert external not in policy.readable_roots
    assert policy.resolve(cwd=checkout).resolve_access(metadata) == FileSystemAccessMode.READ
    assert policy.resolve(cwd=checkout).resolve_access(checkout) == FileSystemAccessMode.READ
    assert policy.allow_network is False


def test_workspace_subdirectory_keeps_captured_owner_and_policy(monkeypatch, tmp_path):
    child = tmp_path / "sub"
    child.mkdir()
    policies = []
    captures = []
    builder = workspace_api_service.readonly_git_policy

    def capture(owner):
        captures.append(owner)
        return builder(owner)

    async def canonical(argv, *, root, cwd, sandbox_policy, timeout):
        assert root == tmp_path and cwd == child
        policies.append(sandbox_policy)
        if "--show-prefix" in argv:
            return _result(argv, "sub/\n")
        return _result(argv, "## main\0 M sub/tracked.txt\0")

    monkeypatch.setattr(git_support, "_run_git", canonical)
    monkeypatch.setattr(workspace_api_service, "readonly_git_policy", capture)
    result = workspace_api_service.workspace_git_status_payload(child, workspace_root=tmp_path)
    assert result["modified"] == ["tracked.txt"]
    assert captures == [tmp_path]
    assert len(policies) == 2 and policies[0] is policies[1]


def test_real_requirements_deny_survives_snapshot_and_metadata_grants(monkeypatch, tmp_path):
    import json
    from backend.workspace import trust
    from backend.sandbox.policy import FileSystemAccessMode

    original = tmp_path / "original"
    checkout = original / ".minicode" / "worktrees" / "target-b"
    checkout.mkdir(parents=True)
    metadata = original / ".git"
    entry = metadata / "worktrees" / "target-b"
    entry.mkdir(parents=True)
    (entry / "gitdir").write_text(str(checkout / ".git"), encoding="utf-8")
    denied_file = checkout / "private.txt"
    requirements = tmp_path / "requirements.toml"
    requirements.write_text(
        "[permissions.filesystem]\ndeny_read = " + json.dumps([str(metadata), str(denied_file)]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MINICODE_REQUIREMENTS_FILE", str(requirements))
    monkeypatch.setattr(trust, "is_workspace_trusted", lambda root: root == original)
    policy = workspace_service.readonly_git_policy(checkout)
    resolved = policy.resolve(cwd=checkout)
    assert policy.readable_roots == (checkout,)
    assert resolved.resolve_access(metadata) is FileSystemAccessMode.DENY
    assert resolved.resolve_access(denied_file) is FileSystemAccessMode.DENY
    assert resolved.resolve_access(checkout) is FileSystemAccessMode.READ
    requirements.write_text(
        "[permissions.filesystem]\ndeny_read = " + json.dumps([str(original)]) + "\n", encoding="utf-8",
    )
    policy = workspace_service.readonly_git_policy(checkout)
    assert policy.readable_roots == ()
    assert policy.resolve(cwd=checkout).resolve_access(checkout) is FileSystemAccessMode.DENY


def test_doctor_git_thread_does_not_block_async_host(monkeypatch, tmp_path):
    from backend.api import routes_health

    entered = threading.Event()
    release = threading.Event()

    def blocking_git(root):
        assert root == tmp_path
        entered.set()
        assert release.wait(5)
        return {"available": True}

    monkeypatch.setattr(routes_health, "get_active_workspace_root", lambda fallback: tmp_path)
    monkeypatch.setattr(routes_health, "build_git_doctor_payload", blocking_git)
    monkeypatch.setattr(routes_health, "build_doctor_payload", lambda **values: values)
    monkeypatch.setattr(routes_health, "_build_llm_status_payload", lambda: {})
    monkeypatch.setattr(routes_health, "_build_capability_status_payload", lambda: {})
    monkeypatch.setattr(routes_health, "_build_preview_doctor_payload", lambda: [])
    monkeypatch.setattr(routes_health, "get_mcp_status", lambda: [])
    monkeypatch.setattr(routes_health._state, "ws_manager", SimpleNamespace(active_count=0, runtime_snapshot=lambda: {}))

    async def oracle():
        pending = asyncio.create_task(routes_health._build_doctor_payload())
        await asyncio.to_thread(entered.wait)
        heartbeat = []
        for _ in range(3):
            await asyncio.sleep(0)
            heartbeat.append(True)
        assert heartbeat == [True, True, True] and not pending.done()
        release.set()
        payload = await pending
        assert payload["workspace_root"] == tmp_path
        assert payload["git_payload"] == {"available": True}

    asyncio.run(oracle())
