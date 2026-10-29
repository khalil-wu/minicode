"""App-owned Git keeps its selected repository and its own temporary indexes."""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import subprocess

import pytest

from backend.agent.environment_selection import bind_tool_execution_environment, close_tool_execution_environment
from backend.runtime_env import ShellEnvironmentPolicy, sanitized_git_env, sanitized_subprocess_env, shell_subprocess_env
from backend.tests.test_executor_tool_admission import executor_host
from backend.workspace.delivery import GitDeliveryRequest, git_delivery_action, git_delivery_status, run_delivery_command
from backend.workspace.executor_git import executor_git_environment, run_executor_git
from backend.workspace.executor_service import ExecutorWorkspaceService
from backend.workspace.worktree import WorktreeManager
from backend.workspace.worktree_snapshots import WorktreeSnapshotStore


@pytest.fixture
def local_git(tmp_path, monkeypatch):
    # Every Git write, config file and remote in this suite belongs to tmp_path.
    # The independent fixture environment also provides an unpoisoned oracle.
    for key in tuple(os.environ):
        if key.upper().startswith("GIT_"):
            monkeypatch.delenv(key)
    home = tmp_path / "git-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    empty_config = home / "empty.gitconfig"
    empty_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    template = home / "empty-template"
    template.mkdir()
    clean_environment = dict(os.environ)

    def git(root, *args, env=None, check=True):
        return subprocess.run(
            ["git", "--no-optional-locks", "-c", f"init.templateDir={template}", *args],
            cwd=root, env=clean_environment if env is None else env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
            check=check, timeout=15,
        )

    return git


def _seed(git, root, filename):
    root.mkdir(exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    for name, value in {
        "user.name": "B6 Fixture", "user.email": "b6@example.invalid",
        "commit.gpgSign": "false", "core.autocrlf": "false",
        "core.fsmonitor": "false", "core.hooksPath": str(root / ".git" / "no-hooks"),
    }.items():
        git(root, "config", "--local", name, value)
    (root / filename).write_text("initial\n", encoding="utf-8")
    git(root, "add", "--", filename)
    git(root, "commit", "-qm", "initial " + filename)
    return git(root, "rev-parse", "HEAD").stdout.strip()


@pytest.mark.parametrize("mixed_case", [False, True])
def test_internal_git_boundary_drops_repository_context_without_changing_user_shell_environment(local_git, monkeypatch, mixed_case):
    if mixed_case and os.name != "nt":
        pytest.skip("Windows environment names are case insensitive")
    inherited = {
        "GIT_DIR": "foreign/.git", "GIT_WORK_TREE": "foreign", "GIT_COMMON_DIR": "foreign/.git",
        "GIT_INDEX_FILE": "foreign/index", "GIT_OBJECT_DIRECTORY": "foreign/objects",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": "foreign/alternate-objects",
        "GIT_CEILING_DIRECTORIES": "foreign/ceiling", "GIT_IMPLICIT_WORK_TREE": "0",
        "GIT_GRAFT_FILE": "foreign/grafts", "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_REPLACE_REF_BASE": "refs/foreign/", "GIT_PREFIX": "foreign/",
        "GIT_SHALLOW_FILE": "foreign/shallow", "GIT_CONFIG": "foreign/config",
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_PARAMETERS": "'core.bare=true'",
    }
    for key, value in inherited.items():
        monkeypatch.setenv(key.swapcase() if mixed_case else key, value)
    retained = {"GIT_CONFIGfoo": "ordinary-user-value", "GIT_SSH_COMMAND": "ssh -F user-config",
        "GIT_AUTHOR_NAME": "User Author", "GIT_OPTIONAL_LOCKS": "0"}
    for key, value in retained.items():
        monkeypatch.setenv(key, value)

    internal = sanitized_git_env()
    generic = sanitized_subprocess_env()
    shell = shell_subprocess_env()
    normalized = {key.upper(): value for key, value in internal.items()}
    generic_names = {key.upper(): value for key, value in generic.items()}
    shell_names = {key.upper(): value for key, value in shell.items()}
    for key, value in inherited.items():
        if key != "GIT_CEILING_DIRECTORIES":
            assert key not in normalized
        assert generic_names[key] == shell_names[key] == value
    assert normalized["GIT_CEILING_DIRECTORIES"] == str(Path.home())
    for key, value in retained.items():
        assert normalized[key.upper()] == generic_names[key.upper()] == shell_names[key.upper()] == value


@pytest.mark.asyncio
async def test_internal_git_keeps_explicit_global_and_system_config_paths(tmp_path, monkeypatch, local_git):
    root = tmp_path / "selected"
    _seed(local_git, root, "selected.txt")
    global_config, system_config = tmp_path / "user-global.gitconfig", tmp_path / "user-system.gitconfig"
    global_config.write_text("[b6]\n\tglobal = user-global\n", encoding="utf-8")
    system_config.write_text("[b6]\n\tsystem = user-system\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(system_config))
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "b6.global")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "injected-global")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'b6.system=injected-system'")

    environment = sanitized_git_env(root)
    assert environment["GIT_CONFIG_GLOBAL"] == str(global_config)
    assert environment["GIT_CONFIG_SYSTEM"] == str(system_config)
    for key, value in (("b6.global", "user-global"), ("b6.system", "user-system")):
        output, code, error = await run_delivery_command(root, ["git", "config", "--get", key])
        assert (output, code, error) == (value, 0, "")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    output, code, error = await run_delivery_command(root, ["git", "config", "--get", "b6.system"], allow_failure=True)
    assert (output, code, error) == ("", 1, "")


@pytest.mark.parametrize("selector", ["repository", "index", "config"])
@pytest.mark.asyncio
async def test_delivery_status_branch_commit_and_local_push_keep_the_selected_repository(tmp_path, monkeypatch, local_git, selector):
    git = local_git
    selected, foreign = tmp_path / "selected", tmp_path / "foreign"
    _seed(git, selected, "selected.txt")
    foreign_head = _seed(git, foreign, "foreign.txt")
    selected_remote, foreign_remote = tmp_path / "selected.git", tmp_path / "foreign.git"
    for root, remote in ((selected, selected_remote), (foreign, foreign_remote)):
        git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
        git(root, "remote", "add", "origin", str(remote))
    (selected / "selected.txt").write_text("selected staged\n", encoding="utf-8")
    git(selected, "add", "--", "selected.txt")
    (selected / "keep-untracked.txt").write_text("not part of this commit\n", encoding="utf-8")
    (foreign / "foreign.txt").write_text("foreign staged\n", encoding="utf-8")
    git(foreign, "add", "--", "foreign.txt")
    foreign_index = (foreign / ".git" / "index").read_bytes()
    foreign_refs = git(foreign, "show-ref").stdout
    values = {
        "repository": {"GIT_DIR": str(foreign / ".git"), "GIT_WORK_TREE": str(foreign),
            "GIT_COMMON_DIR": str(foreign / ".git")},
        "index": {"GIT_INDEX_FILE": str(foreign / ".git" / "index")},
        "config": {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.bare", "GIT_CONFIG_VALUE_0": "true"},
    }[selector]
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    # The repository/index controls reproduce the former routing using only
    # readonly commands. The config case verifies the internal boundary; the
    # bare-repository flag was not an oracle for inherited command config.
    if selector == "repository":
        assert Path(git(selected, "rev-parse", "--show-toplevel", env=sanitized_subprocess_env()).stdout.strip()) == foreign
    elif selector == "index":
        assert git(selected, "ls-files", env=sanitized_subprocess_env()).stdout.strip() == "foreign.txt"
    else:
        internal_environment = sanitized_git_env(selected)
        assert "GIT_CONFIG_COUNT" not in internal_environment
        assert git(selected, "config", "--get", "core.bare", env=internal_environment).stdout.strip() == "false"

    status = await git_delivery_status(selected, include_github=False)
    assert status["is_git_repo"] is True and Path(status["repo_root"]) == selected
    assert status["branch"] == "main" and status["commits"][0]["subject"] == "initial selected.txt"
    await git_delivery_action(selected, GitDeliveryRequest(action="branch", branch="b6-feature", expected_branch="main"))
    await git_delivery_action(selected, GitDeliveryRequest(action="commit", message="selected commit", expected_branch="b6-feature"))
    await git_delivery_action(selected, GitDeliveryRequest(action="push", remote="origin", expected_branch="b6-feature"))

    selected_head = git(selected, "rev-parse", "HEAD").stdout.strip()
    assert git(selected, "branch", "--show-current").stdout.strip() == "b6-feature"
    assert git(selected, "show", "HEAD:selected.txt").stdout == "selected staged\n"
    assert git(selected, "ls-tree", "--name-only", "HEAD").stdout.strip() == "selected.txt"
    assert git(selected, "diff", "--cached", "--name-only").stdout == ""
    assert (selected / "keep-untracked.txt").read_text(encoding="utf-8") == "not part of this commit\n"
    assert git(selected_remote, "rev-parse", "refs/heads/b6-feature").stdout.strip() == selected_head
    assert git(foreign, "rev-parse", "HEAD").stdout.strip() == foreign_head
    assert git(foreign, "show-ref").stdout == foreign_refs
    assert git(foreign, "branch", "--show-current").stdout.strip() == "main"
    assert (foreign / ".git" / "index").read_bytes() == foreign_index
    assert (foreign / "foreign.txt").read_text(encoding="utf-8") == "foreign staged\n"
    assert git(foreign_remote, "show-ref", check=False).returncode == 1


def test_internal_snapshot_preserves_its_explicit_temporary_index_and_both_real_indexes(tmp_path, monkeypatch, local_git):
    git = local_git
    selected, foreign = tmp_path / "selected", tmp_path / "foreign"
    selected_head = _seed(git, selected, "selected.txt")
    foreign_head = _seed(git, foreign, "foreign.txt")
    (selected / "selected.txt").write_text("staged choice\n", encoding="utf-8")
    git(selected, "add", "--", "selected.txt")
    (selected / "selected.txt").write_text("working snapshot\n", encoding="utf-8")
    (selected / "untracked.txt").write_text("snapshot includes this\n", encoding="utf-8")
    selected_index = (selected / ".git" / "index").read_bytes()
    foreign_index = (foreign / ".git" / "index").read_bytes()
    for key, value in {"GIT_DIR": str(foreign / ".git"), "GIT_WORK_TREE": str(foreign),
            "GIT_INDEX_FILE": str(foreign / ".git" / "index")}.items():
        monkeypatch.setenv(key, value)

    store = WorktreeSnapshotStore(tmp_path / "snapshots")
    snapshot = WorktreeManager(selected, snapshot_store=store).snapshot_worktree(selected, conversation_id="b6-owner")
    assert snapshot is not None
    assert store.get(snapshot.id) == snapshot and Path(snapshot.main_repo_path) == selected
    assert git(selected, "show", snapshot.snapshot_sha + ":selected.txt").stdout == "working snapshot\n"
    assert git(selected, "show", snapshot.snapshot_sha + ":untracked.txt").stdout == "snapshot includes this\n"
    assert git(selected, "rev-parse", snapshot.snapshot_ref).stdout.strip() == snapshot.snapshot_sha
    assert git(selected, "rev-parse", "HEAD").stdout.strip() == selected_head
    assert git(foreign, "rev-parse", "HEAD").stdout.strip() == foreign_head
    assert (selected / ".git" / "index").read_bytes() == selected_index
    assert (foreign / ".git" / "index").read_bytes() == foreign_index
    assert git(foreign, "for-each-ref", "refs/minicode/wt-snapshots/").stdout == ""


@pytest.mark.parametrize("executor_host", ["local"], indirect=True)
@pytest.mark.asyncio
async def test_executor_git_reuses_the_rule_and_keeps_only_the_application_index(executor_host, tmp_path, monkeypatch, local_git):
    git = local_git
    selected, foreign = executor_host.workspace, tmp_path / "foreign"
    selected_head = _seed(git, selected, "selected.txt")
    foreign_head = _seed(git, foreign, "foreign.txt")
    (selected / "selected-only.txt").write_text("selected\n", encoding="utf-8")
    selected_index = (selected / ".git" / "index").read_bytes()
    foreign_index = (foreign / ".git" / "index").read_bytes()
    selected_refs, foreign_refs = git(selected, "show-ref").stdout, git(foreign, "show-ref").stdout
    selectors = {"GIT_DIR": str(foreign / ".git"), "GIT_WORK_TREE": str(foreign),
        "GIT_INDEX_FILE": str(foreign / ".git" / "index"), "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.bare", "GIT_CONFIG_VALUE_0": "true"}
    for key, value in selectors.items():
        monkeypatch.setenv(key, value)
    context = bind_tool_execution_environment(executor_host.context, executor_host.environments.primary())
    try:
        async with ExecutorWorkspaceService(context) as service:
            service.config = replace(service.config, shell_environment_policy=ShellEnvironmentPolicy(
                set_values={**selectors, "GIT_CONFIGfoo": "legacy-executor-filtered",
                    "GIT_CONFIG_GLOBAL": "legacy-executor-filtered", "GIT_CONFIG_SYSTEM": "legacy-executor-filtered",
                    "GIT_OPTIONAL_LOCKS": "0"}))
            owned_index = selected / ".git" / "b6-owned-index"
            policy, overrides = executor_git_environment(service, index_file=owned_index)
            assert dict(policy.set_values) == {"GIT_OPTIONAL_LOCKS": "0"}
            assert overrides["GIT_INDEX_FILE"] == service.resolve(str(owned_index)).native
            result = await run_executor_git(service, ["git", "status", "--short"])
            assert result.returncode == 0, result.stderr
            assert b"selected-only.txt" in result.stdout and b"foreign.txt" not in result.stdout
            for argv in (["git", "read-tree", "--empty"], ["git", "add", "--", "selected-only.txt"]):
                result = await run_executor_git(service, argv, write_metadata=True, index_file=owned_index)
                assert result.returncode == 0, result.stderr
            result = await run_executor_git(service, ["git", "ls-files"], index_file=owned_index)
            assert result.returncode == 0 and result.stdout.strip() == b"selected-only.txt"
    finally:
        await close_tool_execution_environment(context)
    assert owned_index.is_file()
    assert (selected / ".git" / "index").read_bytes() == selected_index
    assert (foreign / ".git" / "index").read_bytes() == foreign_index
    assert git(selected, "rev-parse", "HEAD").stdout.strip() == selected_head
    assert git(foreign, "rev-parse", "HEAD").stdout.strip() == foreign_head
    assert git(selected, "show-ref").stdout == selected_refs
    assert git(foreign, "show-ref").stdout == foreign_refs
