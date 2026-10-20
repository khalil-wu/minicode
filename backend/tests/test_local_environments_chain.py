from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import Response

from backend.services import local_environment_settings as settings
from backend.workspace.local_environment_lifecycle import EnvironmentScriptError, EnvironmentScriptResult, execute_environment_script, run_worktree_environment
from backend.workspace.worktree import WorktreeManager
from backend.runtime_env import ShellEnvironmentPolicy


@pytest.fixture(autouse=True)
def isolated_local_environments(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "SELECTIONS_FILE", tmp_path / "selections.json")
    monkeypatch.setattr(settings, "SHELL_ENVIRONMENTS_FILE", tmp_path / "shell.json")
    monkeypatch.setattr("backend.workspace.trust.TRUSTED_WORKSPACES_FILE", tmp_path / "trust.json")


def repository(path):
    path.mkdir()
    for args in [("init",), ("config", "user.email", "fixture@example.invalid"), ("config", "user.name", "Local environment fixture")]:
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    (path / "tracked.txt").write_text("original\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial fixture"], cwd=path, check=True, capture_output=True)
    from backend.workspace.trust import trust_created_task_directory
    trust_created_task_directory(path)
    return path


def configure(root, *, setup="setup-default", cleanup="cleanup-default"):
    document = settings.read_environment(root)
    environment = settings.LocalEnvironment.model_validate({"version": 2, "name": " Build ", "setup": {"script": setup,
        "win32": {"script": "setup-windows"}, "darwin": {"script": ""}}, "cleanup": {"script": cleanup},
        "actions": [{"name": "Run", "command": "echo actual action", "icon": "run"}], "custom": {"keep": True}})
    return settings.save_environment(root, document["config_path"], document["revision"], environment)


def captured_runner(calls, *, code=0):
    def run(command, cwd, variables, *, capture_directory):
        source = capture_directory / ("script.ps1" if os.name == "nt" else "script.sh")
        calls.append((source.read_text(encoding="utf-8-sig"), cwd, variables))
        if code == 0:
            before = {"PATH": "before-path", "DROP_ME": "old", "PWD": "before-cwd"}
            after = {"PATH": "after-path", "PROJECT_ENV": "exported", "PWD": str(cwd), **variables}
            for name, values in [("before.env", before), ("after.env", after)]:
                raw = json.dumps(values) if os.name == "nt" else "\0".join(f"{key}={value}" for key, value in values.items()) + "\0"
                (capture_directory / name).write_text(raw, encoding="utf-8")
        return EnvironmentScriptResult("actual script output\n", "actual stderr\n" if code else "", code)
    return run


def test_source_toml_round_trip_and_conflict_keep_the_original_document(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    saved = configure(root)
    parsed = tomllib.loads(Path(saved["config_path"]).read_text(encoding="utf-8"))
    assert parsed["version"] == 2 and parsed["name"] == "Build"
    assert parsed["setup"]["win32"]["script"] == "setup-windows"
    assert parsed["custom"] == {"keep": True}
    script = settings.LifecycleScript.model_validate(parsed["setup"])
    assert script.for_platform("win32") == "setup-windows"
    assert script.for_platform("darwin") == "setup-default"
    path = Path(saved["config_path"])
    original = path.read_text(encoding="utf-8") + "\n# User edited after opening the form\n"
    path.write_text(original, encoding="utf-8")
    with pytest.raises(settings.EnvironmentConflict, match="draft has been kept"):
        settings.save_environment(root, str(path), saved["revision"], settings.LocalEnvironment.model_validate(saved["environment"]))
    assert path.read_text(encoding="utf-8") == original
    path.write_text("", encoding="utf-8")
    with pytest.raises(ValueError): settings.read_environment(root, str(path))


def test_creation_copies_the_selected_config_runs_in_the_new_worktree_and_captures_exports(tmp_path):
    root = repository(tmp_path / "repo")
    saved = configure(root)
    original = Path(saved["config_path"]).read_text(encoding="utf-8")
    calls = []
    manager = WorktreeManager(root, environment_runner=captured_runner(calls))
    target = root / ".minicode" / "worktrees" / "source-environment"
    assert manager.create_worktree(target, branch="env-create", new_branch=True)
    assert calls[0][0] == ("setup-windows" if sys.platform == "win32" else "setup-default")
    assert calls[0][1] == target
    assert calls[0][2] == {"CODEX_SOURCE_TREE_PATH": str(root), "CODEX_WORKTREE_PATH": str(target)}
    copied = target / ".minicode" / "environments" / "environment.toml"
    assert settings.selected_config(root, target) == copied
    assert copied.read_text(encoding="utf-8") == original
    policy = settings.worktree_shell_policy(ShellEnvironmentPolicy(), target / "child")
    assert policy.set_values == {"PATH": "after-path", "PROJECT_ENV": "exported"}
    assert policy.exclude == ("DROP_ME",)
    assert manager.remove_worktree(target, force=True)
    assert calls[-1][0] == "cleanup-default" and calls[-1][1] == target and calls[-1][2] == {}
    assert Path(saved["config_path"]).read_text(encoding="utf-8") == original
    count = len(calls)
    assert manager.create_worktree(target, branch="env-rollback", new_branch=True, run_environment=False)
    assert manager.remove_worktree(target, force=True, run_environment=False)
    assert len(calls) == count


def test_failed_setup_keeps_real_error_and_the_owned_worktree(tmp_path):
    root = repository(tmp_path / "repo")
    configure(root)
    calls = []
    target = root / ".minicode" / "worktrees" / "failed-environment"
    manager = WorktreeManager(root, environment_runner=captured_runner(calls, code=7))
    with pytest.raises(EnvironmentScriptError, match="actual stderr"):
        manager.create_worktree(target, branch="env-failed", new_branch=True)
    assert target.exists()
    assert settings.selected_config(root, target).is_file()


def test_project_subdirectory_setup_uses_the_new_project_root_not_the_source_tree(tmp_path):
    root = repository(tmp_path / "repo")
    project = root / "web"
    project.mkdir()
    configure(project)
    from backend.workspace.trust import trust_created_task_directory
    trust_created_task_directory(project)
    target = root / ".minicode" / "worktrees" / "nested-project"
    manager = WorktreeManager(root)
    assert manager.create_worktree(target, branch="env-nested", new_branch=True, run_environment=False)
    calls = []
    run_worktree_environment(project, target, "setup", runner=captured_runner(calls))
    assert calls[0][1] == target / "web"
    assert calls[0][2] == {"CODEX_SOURCE_TREE_PATH": str(project), "CODEX_WORKTREE_PATH": str(target / "web")}
    assert settings.selected_config(root, target) == target / "web" / ".minicode" / "environments" / "environment.toml"


def test_settings_can_select_and_edit_the_worktree_copy_without_changing_the_source(tmp_path):
    from backend.api.routes_local_environments import EnvironmentSave, EnvironmentSelection, get_environments, put_environment, put_selection
    root = repository(tmp_path / "repo")
    saved = configure(root)
    target = root / ".minicode" / "worktrees" / "editable-environment"
    manager = WorktreeManager(root, environment_runner=captured_runner([]))
    assert manager.create_worktree(target, branch="env-edit", new_branch=True)
    payload = get_environments(Response(), workspace_root=str(root), execution_root=str(target))
    copy_path = payload["selected_config_path"]
    assert copy_path.startswith(str(target))
    document = get_environments(Response(), workspace_root=str(root), config_path=copy_path, execution_root=str(target))
    changed = settings.LocalEnvironment.model_validate({**document["environment"], "name": "Worktree only"})
    updated = put_environment(EnvironmentSave(workspace_root=str(root), config_path=copy_path, expected_revision=document["revision"], environment=changed, worktree_path=str(target)))
    put_selection(EnvironmentSelection(workspace_root=str(root), config_path=copy_path, worktree_path=str(target)))
    assert updated["environment"]["name"] == "Worktree only"
    assert settings.read_environment(root, saved["config_path"])["environment"]["name"] == "Build"
    request = settings.LocalEnvironmentActionRequest(config_path=copy_path, revision=updated["revision"], index=0)
    assert settings.environment_action(target, request) == "echo actual action"
    request.revision = "stale"
    with pytest.raises(settings.EnvironmentConflict): settings.environment_action(target, request)


def test_setup_shell_delta_reaches_both_local_execution_consumers_and_explicit_values_win(tmp_path, monkeypatch):
    from backend.agent.environment_runtime import resolve_environment_config
    from backend.agent.environment_selection import ThreadEnvironmentConfigDraft
    from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
    from backend.permissions.context import PermissionContext
    from backend.sandbox.policy import PermissionProfile, SandboxPolicy
    from backend.tools.command_support import _workspace_sandbox_policy

    target = tmp_path / "worktree"
    target.mkdir()
    settings.store_worktree_shell_environment(target, {"set": {"VIRTUAL_ENV": "owned-venv", "PATH": "exported-path"}, "exclude": ["REMOVED_BY_SETUP"]})
    base = ShellEnvironmentPolicy(set_values={"PATH": "explicit-path"})
    snapshot = SandboxPolicy(workspace_root=target, shell_environment_policy=base)
    policy = _workspace_sandbox_policy(target, SimpleNamespace(sandbox_policy=snapshot), timeout=10, env_overrides={"APP_ENV": "command-value"})
    assert policy.shell_environment_policy.set_values == {"VIRTUAL_ENV": "owned-venv", "PATH": "explicit-path"}
    assert policy.env_overrides["APP_ENV"] == "command-value"
    fallback = _workspace_sandbox_policy(target, None, timeout=10)
    assert fallback.shell_environment_policy.set_values["VIRTUAL_ENV"] == "owned-venv"
    capture = AsyncMock(return_value=PermissionProfile.disabled())
    monkeypatch.setattr("backend.agent.environment_runtime.capture_environment_permission_profile", capture)
    stack = ConfigLayerStack((ConfigLayer(ConfigLayerSource("user", file=str(tmp_path / "settings.json")), {}),))
    draft = ThreadEnvironmentConfigDraft(PermissionContext(), stack, shell_environment_policy=base)
    config = asyncio.run(resolve_environment_config(SimpleNamespace(environment_id="local", cwd=target.as_uri()), draft,
        SimpleNamespace(filesystem=object()), {"platformOs": "windows" if os.name == "nt" else "linux"}))
    assert config.shell_environment_policy.set_values["VIRTUAL_ENV"] == "owned-venv"
    remote = asyncio.run(resolve_environment_config(SimpleNamespace(environment_id="remote", cwd="file:///remote/project"), draft,
        SimpleNamespace(filesystem=object()), {"platformOs": "linux"}))
    assert "VIRTUAL_ENV" not in remote.shell_environment_policy.set_values


def test_read_only_owner_does_not_launch_a_lifecycle_shell(tmp_path, monkeypatch):
    from backend.permissions.context import PermissionContext, ToolExecutionContext
    from backend.sandbox.policy import sandbox_policy_from_config_snapshot
    root = tmp_path / "workspace"
    root.mkdir()
    policy = sandbox_policy_from_config_snapshot(root, sandbox_mode="read-only")
    context = ToolExecutionContext(permission=PermissionContext(), workspace_root=root, sandbox_policy=policy)
    monkeypatch.setattr("backend.sandbox.runner.SandboxRunner", lambda *_: pytest.fail("Readonly owner launched a script"))
    with pytest.raises(PermissionError, match="write permission"):
        asyncio.run(execute_environment_script("never executed", root, {}, context=context))
