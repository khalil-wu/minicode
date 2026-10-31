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
def test_ui_workspace_status_and_diff_share_host_metadata_boundary(monkeypatch, tmp_path, unborn):
    calls = []

    def host_metadata(root, *args, timeout=10):
        assert root == tmp_path
        assert timeout == 10
        argv = ["git", *args]
        args = list(args)
        calls.append(tuple(args))
        if args == ["rev-parse", "--show-prefix"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if args[:2] == ["status", "--porcelain=v1"]:
            return subprocess.CompletedProcess(argv, 0, "## main\0 M tracked.txt\0?? new.txt\0", "")
        if args == ["rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(argv, 0, str(tmp_path) + "\n", "")
        if args == ["rev-parse", "--verify", "--quiet", "--end-of-options", "HEAD"]:
            return subprocess.CompletedProcess(argv, 1 if unborn else 0, "" if unborn else "head\n", "")
        if args == ["hash-object", "-t", "tree", "--stdin"]:
            return subprocess.CompletedProcess(argv, 0, "empty-tree\n", "")
        if args[0] == "ls-files":
            return subprocess.CompletedProcess(argv, 0, "new.txt\0", "")
        if args[0] == "diff":
            assert "--no-textconv" in args and "--no-ext-diff" in args
            return subprocess.CompletedProcess(argv, 1 if "--no-index" in args else 0, "new patch\n" if "--no-index" in args else "tracked patch\n", "")
        raise AssertionError(args)

    monkeypatch.setattr(workspace_api_service, "run_ui_git_metadata", host_metadata)
    status = workspace_api_service.workspace_git_status_payload(tmp_path)
    assert status == {"is_git_repo": True, "branch": "main", "modified": ["tracked.txt"], "staged": [], "untracked": ["new.txt"]}
    assert workspace_api_service.workspace_git_diff_payload(tmp_path, "") == {"is_git_repo": True, "diff": "tracked patch\nnew patch\n"}
    assert ("rev-parse", "--verify", "--quiet", "--end-of-options", "HEAD") in calls
    assert any(call[0] == "diff" and call[-3:] == ("empty-tree" if unborn else "head", "--", ".") for call in calls)
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
            return _result(argv, "# branch.oid abc\0# branch.head main\0" if "--porcelain=v2" in argv else "")
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
    monkeypatch.setattr(health_service, "run_ui_git_metadata", lambda root, *args, **kwargs:
        subprocess.CompletedProcess(["git", *args], 0, "main\n" if args[0] == "branch" else "", ""))
    assert health_service.build_git_doctor_payload(main) == {"available": True, "branch": "main", "changed": 0, "clean": True, "error": ""}
    conversation = SimpleNamespace(id="target-b", git_isolated=False, workspace_root=str(main), worktree_path="", git_branch="")
    preflight = handoff.build_handoff_preflight(
        conversation, target="worktree", conversation_repo=SimpleNamespace(list_conversations=lambda: []),
        main_worktree_root=workspace_service.main_worktree_root, has_running_turn=False,
    )
    assert preflight["allowed"] is True
    assert preflight["source"]["path"] == str(main)
    assert preflight["conversation_id"] == "target-b"
    assert ("show-ref", "--verify", "refs/heads/codex/target-b") in calls


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
        if args[:2] == ("stash", "list"):
            return True, f"{'a' * 40}\0On main: {marker}\n"
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
    assert reads == []
    assert [args[:3] if args[0] == "switch" else args[:2] for args in writes] == [("stash", "push"), ("stash", "list"), ("stash", "apply"), ("switch", "--no-overwrite-ignore", "next"), ("switch", "--no-overwrite-ignore", "main"), ("branch", "-D")]


def test_host_git_and_model_namespace_failures_remain_distinct(monkeypatch, tmp_path):
    async def canonical(argv, *, root, cwd, sandbox_policy, timeout):
        raise SandboxUnavailableError("actual namespace unavailable")

    monkeypatch.setattr(git_support, "_run_git", canonical)
    def host_unavailable(*args, **kwargs):
        raise OSError("host Git unavailable")
    monkeypatch.setattr(workspace_api_service, "run_ui_git_metadata", host_unavailable)
    monkeypatch.setattr(health_service, "run_ui_git_metadata", host_unavailable)
    assert workspace_api_service.workspace_git_status_payload(tmp_path)["error"] == "host Git unavailable"
    assert workspace_api_service.workspace_git_diff_payload(tmp_path, "")["error"] == "host Git unavailable"
    doctor = health_service.build_git_doctor_payload(tmp_path)
    assert doctor["available"] is False and doctor["clean"] is False
    assert handoff._workspace_snapshot(tmp_path).status == "<status unavailable>"


def test_doctor_does_not_report_a_failed_git_status_as_clean(monkeypatch, tmp_path):
    def git_metadata(_root, *args, **_kwargs):
        if args[0] == "branch":
            return subprocess.CompletedProcess(["git", *args], 0, "main\n", "")
        return subprocess.CompletedProcess(["git", *args], 1, "", "status unavailable")

    monkeypatch.setattr(health_service, "run_ui_git_metadata", git_metadata)
    result = health_service.build_git_doctor_payload(tmp_path)
    assert result["available"] is True
    assert result["clean"] is False
    assert result["error"] == "status unavailable"


@pytest.mark.parametrize("absolute", [False, True])
def test_ui_repository_path_outputs_are_host_paths(monkeypatch, tmp_path, absolute):
    common_dir = str(tmp_path / ".git") if absolute else ".git"
    monkeypatch.setattr(workspace_api_service, "run_ui_git_metadata", lambda root, *args:
        subprocess.CompletedProcess(["git", *args], 0, common_dir + "\n", ""))
    assert workspace_api_service.resolve_git_common_dir(tmp_path) == tmp_path / ".git"


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


def test_ui_workspace_subdirectory_keeps_requested_host_root_and_relative_paths(monkeypatch, tmp_path):
    child = tmp_path / "sub"
    child.mkdir()
    calls = []
    def host_metadata(root, *args):
        assert root == child
        calls.append(args)
        stdout = "sub/\n" if "--show-prefix" in args else "## main\0 M sub/tracked.txt\0"
        return subprocess.CompletedProcess(["git", *args], 0, stdout, "")
    monkeypatch.setattr(workspace_api_service, "run_ui_git_metadata", host_metadata)
    result = workspace_api_service.workspace_git_status_payload(child, workspace_root=tmp_path)
    assert result["modified"] == ["tracked.txt"]
    assert len(calls) == 2
    assert calls[1][-2:] == ("--", ".")


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


def _real_repository(root, *, branch="main"):
    from backend.runtime_env import sanitized_git_env
    from backend.workspace.trust import trust_created_task_directory

    root.mkdir()
    env = sanitized_git_env(root)
    for args in (("init", "-b", branch), ("config", "user.name", "Readonly fixture"), ("config", "user.email", "offline@example.invalid")):
        subprocess.run(["git", *args], cwd=root, env=env, check=True, capture_output=True)
    (root / "tracked.txt").write_text("initial content\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, env=env, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial fixture"], cwd=root, env=env, check=True, capture_output=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, env=env, check=True, capture_output=True, text=True).stdout.strip()
    trust_created_task_directory(root)
    return head


def test_cold_subdirectory_first_git_read_uses_its_captured_policy_and_authorized_parent_metadata(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from backend.sandbox.policy import FileSystemAccessMode
    from backend import config

    repo = tmp_path / "repo"
    head = _real_repository(repo)
    selected = repo / " sub 中文"
    selected.mkdir()
    (selected / "new 中文 file.txt").write_text("untracked\n", encoding="utf-8")
    capture = Mock(wraps=config.load_config_layer_stack)
    monkeypatch.setattr(config, "load_config_layer_stack", capture)
    policy = workspace_service.readonly_git_policy(selected)
    assert policy.workspace_root == selected
    assert policy.readable_roots == (selected, repo / ".git", repo)
    resolved = policy.resolve(cwd=selected)
    assert resolved.resolve_access(repo / ".git") is FileSystemAccessMode.READ
    assert resolved.resolve_access(repo) is FileSystemAccessMode.READ
    assert resolved.resolve_access(selected) is FileSystemAccessMode.READ
    read = Mock(wraps=workspace_service.run_readonly_git)
    monkeypatch.setattr(workspace_service, "run_readonly_git", read)
    assert workspace_service.main_worktree_root(selected) == repo
    assert read.call_count == 0
    assert all(call.kwargs["cwd"] == selected for call in capture.call_args_list)
    result = workspace_service.run_readonly_git(selected, "status", "--porcelain=v2", "-z", "--branch", "--untracked-files=all", sandbox_policy=policy)
    assert result.returncode == 0, result.stderr
    assert f"# branch.oid {head}\0" in result.stdout
    assert "# branch.head main\0" in result.stdout
    assert "?  sub 中文/new 中文 file.txt\0" in result.stdout
    assert read.call_count == 1


@pytest.mark.parametrize("deny_ancestor", [False, True])
def test_parent_metadata_admission_never_reopens_the_selected_policy_deny(tmp_path, monkeypatch, deny_ancestor):
    import json
    from backend.sandbox.policy import FileSystemAccessMode

    repo = tmp_path / "repo"
    _real_repository(repo)
    selected = repo / "selected"
    selected.mkdir()
    requirements = tmp_path / "requirements.toml"
    denied = repo if deny_ancestor else repo / ".git"
    requirements.write_text("[permissions.filesystem]\ndeny_read = " + json.dumps([str(denied)]) + "\n", encoding="utf-8")
    monkeypatch.setenv("MINICODE_REQUIREMENTS_FILE", str(requirements))
    policy = workspace_service.readonly_git_policy(selected)
    resolved = policy.resolve(cwd=selected)
    assert repo / ".git" not in policy.readable_roots
    assert resolved.resolve_access(repo / ".git") is FileSystemAccessMode.DENY
    if deny_ancestor:
        assert policy.readable_roots == ()
        assert resolved.resolve_access(selected) is FileSystemAccessMode.DENY
    else:
        assert policy.readable_roots == (selected,)
        result = workspace_service.run_readonly_git(selected, "status", "--porcelain=v2", "--branch", sandbox_policy=policy)
        assert result.returncode != 0, result.stdout


def test_subdirectory_gitfile_cannot_admit_external_metadata_or_fall_through_to_outer_repo(tmp_path):
    from backend.workspace.trust import trust_created_task_directory

    outer = tmp_path / "outer"
    (outer / ".git").mkdir(parents=True)
    inner = outer / "inner"
    selected = inner / "selected"
    selected.mkdir(parents=True)
    external = tmp_path / "external" / ".git"
    external.mkdir(parents=True)
    (inner / ".git").write_text(f"gitdir: {external.as_posix()}\n", encoding="utf-8")
    trust_created_task_directory(outer)
    policy = workspace_service.readonly_git_policy(selected)
    assert policy.readable_roots == (selected,)
    assert external not in policy.readable_roots
    assert outer / ".git" not in policy.readable_roots


def test_selected_folder_trust_does_not_admit_an_untrusted_ancestor_repository(tmp_path):
    from backend.workspace.trust import trust_created_task_directory

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    selected = repo / "selected"
    selected.mkdir()
    trust_created_task_directory(selected)
    assert workspace_service.readonly_git_policy(selected).readable_roots == (selected,)


def test_parent_metadata_discovery_respects_the_existing_home_ceiling(tmp_path, monkeypatch):
    from backend.workspace.trust import trust_created_task_directory

    home = tmp_path / "home"
    (home / ".git").mkdir(parents=True)
    selected = home / "selected"
    selected.mkdir()
    trust_created_task_directory(home)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    assert workspace_service.readonly_git_policy(selected).readable_roots == (selected,)


def test_cold_subdirectory_parent_admission_keeps_the_existing_repository_env_filter(tmp_path, monkeypatch):
    repo = tmp_path / "target"
    expected_head = _real_repository(repo)
    selected = repo / "selected"
    selected.mkdir()
    (selected / "target-only.txt").write_text("actual target\n", encoding="utf-8")
    foreign = tmp_path / "foreign"
    foreign_head = _real_repository(foreign, branch="foreign")
    foreign_index = (foreign / ".git" / "index").read_bytes()
    monkeypatch.setenv("GIT_DIR", str(foreign / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(foreign))
    monkeypatch.setenv("GIT_INDEX_FILE", str(foreign / ".git" / "index"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.worktree")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(foreign))
    policy = workspace_service.readonly_git_policy(selected)
    result = workspace_service.run_readonly_git(selected, "status", "--porcelain=v2", "-z", "--branch", "--untracked-files=all", sandbox_policy=policy)
    assert result.returncode == 0, result.stderr
    assert f"# branch.oid {expected_head}\0" in result.stdout
    assert "# branch.head main\0" in result.stdout
    assert "? selected/target-only.txt\0" in result.stdout
    assert "foreign" not in result.stdout
    assert (foreign / ".git" / "index").read_bytes() == foreign_index
    assert (foreign / ".git" / "refs" / "heads" / "foreign").read_text(encoding="utf-8").strip() == foreign_head


def test_materialized_parent_worktree_read_keeps_selected_and_sibling_file_denies(tmp_path, monkeypatch):
    import json
    from backend.runtime_env import sanitized_git_env
    from backend.sandbox.policy import FileSystemAccessMode

    repo = tmp_path / "repo"
    _real_repository(repo)
    selected = repo / "selected"
    selected.mkdir()
    public = selected / "public.txt"
    public.write_text("readable fixture\n", encoding="utf-8")
    selected_private = selected / "private.txt"
    selected_private.write_text("private fixture\n", encoding="utf-8")
    sibling_private = repo / "sibling-private.txt"
    sibling_private.write_text("sibling private fixture\n", encoding="utf-8")
    expected = subprocess.run(["git", "hash-object", "--no-filters", "--", public.name], cwd=selected,
        env=sanitized_git_env(selected), capture_output=True, check=True, text=True).stdout.strip()
    requirements = tmp_path / "requirements.toml"
    requirements.write_text("[permissions.filesystem]\ndeny_read = " + json.dumps([str(selected_private), str(sibling_private)]) + "\n", encoding="utf-8")
    monkeypatch.setenv("MINICODE_REQUIREMENTS_FILE", str(requirements))
    policy = workspace_service.readonly_git_policy(selected)
    resolved = policy.resolve(cwd=selected)
    assert policy.workspace_root == selected and repo in policy.readable_roots
    assert resolved.resolve_access(repo) is FileSystemAccessMode.READ
    assert resolved.resolve_access(public) is FileSystemAccessMode.READ
    assert resolved.resolve_access(selected_private) is FileSystemAccessMode.DENY
    assert resolved.resolve_access(sibling_private) is FileSystemAccessMode.DENY
    allowed = workspace_service.run_readonly_git(selected, "hash-object", "--no-filters", "--", "public.txt", sandbox_policy=policy)
    assert allowed.returncode == 0 and allowed.stdout.strip() == expected, allowed.stderr
    for relative in ("private.txt", "../sibling-private.txt"):
        denied = workspace_service.run_readonly_git(selected, "hash-object", "--no-filters", "--", relative, sandbox_policy=policy)
        assert denied.returncode != 0, {"path": relative, "stdout": denied.stdout, "stderr": denied.stderr}
