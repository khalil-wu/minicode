from __future__ import annotations

from pathlib import Path
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.services import conversation_worktree_handoff_service as handoff
from backend.services.worktree_settings_service import WorktreePreferences, managed_worktree_record, update_worktree_preferences
from backend.services.workspace_service import readonly_git_policy
from backend.workspace.trust import trust_created_task_directory
from backend.workspace.worktree import WorktreeManager


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, check=check, capture_output=True, text=True, encoding="utf-8")


def repository(path: Path, *, unborn: bool = False) -> Path:
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.email", "offline@example.invalid")
    git(path, "config", "user.name", "Handoff snapshot fixture")
    if not unborn:
        (path / ".gitignore").write_text("*.tmp\nignored directory/\n", encoding="utf-8")
        (path / "中文 tracked file.txt").write_text("original\n", encoding="utf-8")
        (path / "ignored directory").mkdir()
        (path / "ignored directory" / "nested").mkdir()
        (path / "ignored directory" / "nested" / "中文 ignored.txt").write_text("ignored\n", encoding="utf-8")
        (path / "中文 ignored.tmp").write_text("ignored\n", encoding="utf-8")
        git(path, "add", ".")
        git(path, "commit", "-m", "Initial fixture")
    trust_created_task_directory(path)
    return path


def trace_canonical_reads(monkeypatch):
    actual = handoff.run_readonly_git
    receipts = []

    def read(root, *args, **kwargs):
        policy = kwargs.get("sandbox_policy")
        receipt = {"root": str(root), "args": args, "policy_root": str(policy.workspace_root),
            "readable_roots": [str(path) for path in policy.readable_roots],
            "permission_profile": repr(policy.permission_profile)}
        receipts.append(receipt)
        try:
            result = actual(root, *args, **kwargs)
        except Exception as error:
            receipt.update(error_type=type(error).__name__, error=str(error))
            raise
        receipt.update(returncode=result.returncode, stderr=result.stderr, stdout=result.stdout)
        return result

    run = Mock(side_effect=read)
    monkeypatch.setattr(handoff, "run_readonly_git", run)
    return run, receipts


def managed_linked_checkout(root: Path, pool: Path, *, branch: str) -> Path:
    update_worktree_preferences(WorktreePreferences(worktree_root=str(pool)))
    pool.mkdir()
    linked = pool / "linked"
    assert WorktreeManager(root).create_worktree(linked, branch=branch, new_branch=True, run_environment=False)
    record = managed_worktree_record(linked)
    assert record and Path(record["repo_root"]) == root
    return linked


@pytest.mark.parametrize("state", ["clean", "unstaged", "staged", "deleted", "renamed", "untracked", "unborn", "detached", "unicode-branch", "unmerged"])
def test_one_canonical_snapshot_matches_real_head_branch_dirty_and_ignored_oracles(tmp_path, monkeypatch, state):
    root = repository(tmp_path / "中文 workspace", unborn=state == "unborn")
    tracked = root / "中文 tracked file.txt"
    if state in {"unstaged", "staged"}:
        tracked.write_text("modified\n", encoding="utf-8")
        if state == "staged":
            git(root, "add", tracked.name)
    elif state == "deleted":
        tracked.unlink()
    elif state == "renamed":
        git(root, "mv", tracked.name, "renamed 中文 name.txt")
    elif state in {"untracked", "unborn"}:
        (root / "new 中文 space.txt").write_text("untracked\n", encoding="utf-8")
    elif state == "detached":
        git(root, "switch", "--detach", "HEAD")
    elif state == "unicode-branch":
        git(root, "switch", "-c", "feature/中文")
    elif state == "unmerged":
        git(root, "switch", "-c", "other")
        tracked.write_text("other branch\n", encoding="utf-8")
        git(root, "commit", "-am", "Other branch")
        git(root, "switch", "main")
        tracked.write_text("main branch\n", encoding="utf-8")
        git(root, "commit", "-am", "Main branch")
        assert git(root, "merge", "other", check=False).returncode != 0
    policy = readonly_git_policy(root)
    run = Mock(wraps=handoff.run_readonly_git)
    monkeypatch.setattr(handoff, "run_readonly_git", run)

    snapshot = handoff._workspace_snapshot(root, sandbox_policy=policy)

    assert len(run.call_args_list) == 1
    assert run.call_args.args == (root, "status", "--porcelain=v2", "-z", "--branch", "--untracked-files=all", "--ignored=traditional")
    assert run.call_args.kwargs["sandbox_policy"] is policy
    expected_head = git(root, "rev-parse", "HEAD", check=False)
    assert snapshot.head == (expected_head.stdout.strip() if expected_head.returncode == 0 else "")
    assert snapshot.branch == git(root, "branch", "--show-current").stdout.strip()
    assert bool(snapshot.status) == bool(git(root, "status", "--porcelain=v1", "--untracked-files=all").stdout.strip())
    expected_ignored = git(root, "ls-files", "--others", "--ignored", "--exclude-standard", "-z").stdout.rstrip("\0").split("\0")
    assert snapshot.ignored == [name for name in expected_ignored if name][:20]
    if state == "renamed":
        assert "renamed 中文 name.txt\0中文 tracked file.txt" in snapshot.status
        assert snapshot.status.startswith("2 R.")
    if state == "unmerged":
        assert snapshot.status.startswith("u ")
    if state in {"untracked", "unborn"}:
        assert "? new 中文 space.txt" in snapshot.status


@pytest.mark.parametrize("isolated", [False, True])
def test_preflight_reads_each_distinct_root_once_and_reuses_local_source_snapshot(tmp_path, monkeypatch, isolated):
    main = tmp_path / "main"
    source = tmp_path / "linked" if isolated else main
    main.mkdir()
    if isolated:
        source.mkdir()
    policies = {root: object() for root in {main, source}}
    monkeypatch.setattr(handoff, "readonly_git_policy", lambda root: policies[root])
    calls = []

    def read(root, *args, **kwargs):
        calls.append((root, args))
        assert kwargs["sandbox_policy"] is policies[root]
        if args[0] == "status":
            return subprocess.CompletedProcess(["git", *args], 0, f"# branch.oid {root.name}-head\0# branch.head {root.name}-branch\0! {root.name}.tmp\0", "")
        assert args[0] == "show-ref"
        return subprocess.CompletedProcess(["git", *args], 1, "", "ref absent")

    monkeypatch.setattr(handoff, "run_readonly_git", read)
    conversation = SimpleNamespace(id="snapshot-owner", workspace_root=str(source), worktree_path=str(source) if isolated else "",
        git_isolated=isolated, git_branch=f"{source.name}-branch")
    result = handoff.build_handoff_preflight(conversation, target="local" if isolated else "worktree",
        conversation_repo=SimpleNamespace(list_conversations=lambda: []), main_worktree_root=lambda _root: main, has_running_turn=False)
    status_roots = [root for root, args in calls if args[0] == "status"]
    assert status_roots == ([source, main] if isolated else [main])
    for root, args in calls:
        if args[0] == "status":
            assert ("--ignored=traditional" in args) is (root == source)
    assert len(calls) == 2
    assert result["source"] == {"path": str(source), "branch": f"{source.name}-branch", "head": f"{source.name}-head"}
    assert result["main_checkout"] == {"path": str(main), "branch": "main-branch", "head": "main-head"}
    assert next(check["details"]["sample"] for check in result["checks"] if check["code"] == "ignored.files") == [f"{source.name}.tmp"]


def test_main_subdirectory_ignored_sample_stays_cwd_relative_and_does_not_count_outside_files(tmp_path, monkeypatch, record_property):
    root = repository(tmp_path / "repo")
    sub = root / " sub 中文"
    sub.mkdir()
    for index in range(25):
        (root / f"outside-{index:02}.tmp").write_text("ignored outside\n", encoding="utf-8")
        (sub / f"inside-{index:02}.tmp").write_text("ignored inside\n", encoding="utf-8")
    run, receipts = trace_canonical_reads(monkeypatch)
    conversation = SimpleNamespace(id="subdir-owner", workspace_root=str(sub), worktree_path="", git_isolated=False, git_branch="main")
    result = handoff.build_handoff_preflight(conversation, target="worktree", conversation_repo=SimpleNamespace(list_conversations=lambda: []),
        main_worktree_root=lambda _root: root, has_running_turn=False)
    trace = {"preflight": result, "canonical_reads": receipts}
    trace_json = json.dumps(trace, ensure_ascii=False, indent=2)
    trace_path = tmp_path / "handoff-canonical-trace.json"
    trace_path.write_text(trace_json, encoding="utf-8")
    record_property("handoff_canonical_trace", trace_json)
    record_property("handoff_canonical_trace_path", str(trace_path))
    assert result["allowed"], {"preflight": result, "canonical_reads": receipts}
    ignored_checks = [check for check in result["checks"] if check["code"] == "ignored.files"]
    assert ignored_checks, {"preflight": result, "canonical_reads": receipts}
    sample = ignored_checks[0]["details"]["sample"]
    expected = git(sub, "ls-files", "--others", "--ignored", "--exclude-standard", "-z").stdout.rstrip("\0").split("\0")[:20]
    assert sample == expected
    assert len(sample) == 20 and all(name.startswith("inside-") for name in sample)
    assert sum(call.args[1] == "status" for call in run.call_args_list) == 2
    assert not any(call.args[1] == "rev-parse" for call in run.call_args_list)
    assert "--ignored=traditional" not in next(call.args for call in run.call_args_list if call.args[0] == root and call.args[1] == "status")


def test_linked_checkout_subdirectory_reads_its_real_prefix_without_losing_leading_spaces(tmp_path, monkeypatch):
    root = repository(tmp_path / "repo")
    sub_name = " sub 中文"
    (root / sub_name).mkdir()
    (root / sub_name / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    git(root, "add", sub_name)
    git(root, "commit", "-m", "Subdirectory fixture")
    linked = managed_linked_checkout(root, tmp_path / "managed-pool", branch="linked-branch")
    sub = linked / sub_name
    (linked / "outside.tmp").write_text("ignored outside\n", encoding="utf-8")
    (sub / "中文 inside.tmp").write_text("ignored inside\n", encoding="utf-8")
    run, receipts = trace_canonical_reads(monkeypatch)
    policy = readonly_git_policy(linked)
    assert root / ".git" in policy.readable_roots

    snapshot = handoff._workspace_snapshot(sub, sandbox_policy=policy, relative_prefix=None)

    assert snapshot.status != "<status unavailable>", receipts
    assert snapshot.head == git(linked, "rev-parse", "HEAD").stdout.strip(), receipts
    assert snapshot.branch == "linked-branch"
    assert snapshot.ignored == ["中文 inside.tmp"]
    assert [call.args[1:] for call in run.call_args_list] == [
        ("rev-parse", "--show-prefix"),
        ("status", "--porcelain=v2", "-z", "--branch", "--untracked-files=all", "--ignored=traditional"),
    ]


def test_return_local_does_not_scan_main_ignored_inventory_or_project_its_sample(tmp_path, monkeypatch):
    root = repository(tmp_path / "repo")
    linked = managed_linked_checkout(root, tmp_path / "managed-pool", branch="task-branch")
    (linked / "source.tmp").write_text("ignored task content\n", encoding="utf-8")
    for index in range(25):
        (root / f"main-{index:02}.tmp").write_text("ignored main content\n", encoding="utf-8")
    run, receipts = trace_canonical_reads(monkeypatch)
    conversation = SimpleNamespace(id="return-owner", workspace_root=str(linked), worktree_path=str(linked), git_isolated=True, git_branch="task-branch")

    result = handoff.build_handoff_preflight(conversation, target="local", conversation_repo=SimpleNamespace(list_conversations=lambda: []),
        main_worktree_root=lambda _root: root, has_running_turn=False)

    assert result["allowed"], {"preflight": result, "canonical_reads": receipts}
    assert next(check["details"]["sample"] for check in result["checks"] if check["code"] == "ignored.files") == ["source.tmp"]
    assert len(run.call_args_list) == 2
    source_args, main_args = [call.args for call in run.call_args_list]
    assert source_args[0] == linked and "--ignored=traditional" in source_args
    assert main_args[0] == root and "--ignored=traditional" not in main_args


def test_one_failed_snapshot_keeps_preflight_blocked_without_publishing_clean_state(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    calls = []

    def failed(root, *args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(["git", *args], 128, "", "actual status error")

    monkeypatch.setattr(handoff, "run_readonly_git", failed)
    conversation = SimpleNamespace(id="snapshot-error", workspace_root=str(root), worktree_path="", git_isolated=False, git_branch="")
    result = handoff.build_handoff_preflight(conversation, target="worktree", conversation_repo=SimpleNamespace(list_conversations=lambda: []),
        main_worktree_root=lambda _root: root, has_running_turn=False)
    assert not result["allowed"]
    assert any(check["code"] == "source.status_unavailable" for check in result["checks"])
    assert not any(check["code"] == "ignored.files" for check in result["checks"])
    assert result["source"]["head"] == result["source"]["branch"] == ""
    assert sum(args[0] == "status" for args in calls) == 1
