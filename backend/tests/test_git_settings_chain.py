import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException, Response

from backend.api import routes_git_settings
from backend.services import git_settings_service as settings
from backend.workspace import delivery, executor_delivery, git_message_generation as generation
from backend.workspace.executor_editor import ExecutorEditorService
from backend.sandbox.policy import SandboxEnforcement


@pytest.fixture(autouse=True)
def isolated_preferences(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "GIT_PREFERENCES_FILE", tmp_path / "git-preferences.json")


def test_saved_prefix_reaches_generated_branch_and_cleanup_keeps_its_original_name(tmp_path, monkeypatch):
    from backend.agent import worktree

    assert routes_git_settings.get_git_settings(Response()).branch_prefix == "codex/"
    routes_git_settings.put_git_settings(settings.GitPreferences(branch_prefix="feature/", always_force_push=True), Response())
    routes_git_settings.put_git_settings(settings.GitPreferences(commit_instructions="Use Chinese"), Response())
    saved = routes_git_settings.get_git_settings(Response())
    assert saved.branch_prefix == "feature/" and saved.always_force_push
    assert saved.commit_instructions == "Use Chinese"
    commands = []
    monkeypatch.setattr(worktree, "find_git_root", lambda root: tmp_path)
    monkeypatch.setattr(worktree, "_git_output", lambda *args: "frozen-head")
    monkeypatch.setattr(worktree, "_ensure_excluded", lambda root: None)

    def git(root, *args):
        commands.append(args)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(worktree, "_git", git)
    created, error = worktree.create_agent_worktree("agent-test", tmp_path)
    assert not error and created.branch == "feature/agent-test"
    assert commands[0][:4] == ("worktree", "add", "-b", "feature/agent-test")
    routes_git_settings.put_git_settings(settings.GitPreferences(branch_prefix="next/"), Response())
    assert worktree.remove_agent_worktree(created)
    assert commands[-1] == ("branch", "-D", "feature/agent-test")
    worktree._ACTIVE_WORKTREE_PATHS.discard(worktree.canonical_file_path_key(created.worktree_path))


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["local", "remote"])
async def test_confirmed_delivery_flags_reach_both_real_consumers(tmp_path, monkeypatch, target):
    saved = routes_git_settings.put_git_settings(settings.GitPreferences(always_force_push=True, create_draft_pull_requests=False), Response())
    status = {"is_git_repo": True, "branch": "feature", "remotes": ["origin"],
        "github": {"eligible": True, "available": True, "authenticated": True}}
    commands = []
    if target == "local":
        monkeypatch.setattr(delivery, "git_delivery_status", AsyncMock(return_value=status))
        monkeypatch.setattr(delivery, "github_repository_context", AsyncMock(return_value={"eligible": True}))
        monkeypatch.setattr(delivery, "github_cli_command", lambda: "gh")
        monkeypatch.setattr(delivery, "github_connection_status", AsyncMock(return_value={"authenticated": True}))

        async def command(root, argv, **kwargs):
            commands.append(argv)
            return "https://github.com/org/repo/pull/1", 0, ""

        monkeypatch.setattr(delivery, "run_delivery_command", command)
        action = lambda request: delivery.git_delivery_action(tmp_path, request)
    else:
        service = SimpleNamespace(identity=lambda: {"environment_id": "original"})
        monkeypatch.setattr(executor_delivery, "require_workspace_registration", lambda owner, registration: None)
        monkeypatch.setattr(executor_delivery, "target_programs", AsyncMock(return_value={"git": "git", "gh": "gh"}))
        monkeypatch.setattr(executor_delivery, "executor_delivery_status", AsyncMock(return_value=status))
        monkeypatch.setattr(executor_delivery, "executor_git_environment", lambda owner: ("original-policy", {}))
        monkeypatch.setattr(executor_delivery, "executor_git_write_scope", AsyncMock(return_value=SimpleNamespace(
            permission_profile=SimpleNamespace(enforcement=SandboxEnforcement.DISABLED))))

        async def command(owner, argv, **kwargs):
            assert owner is service
            commands.append(argv)
            return SimpleNamespace(stdout="https://github.com/org/repo/pull/1")

        monkeypatch.setattr(executor_delivery, "run_workbench_command", command)
        action = lambda request: executor_delivery.executor_delivery_action(service, request)
    await action(delivery.GitDeliveryRequest(action="push", remote="origin", expected_branch="feature", force_with_lease=saved.always_force_push))
    assert commands[-1] == ["git", "push", "--force-with-lease", "--set-upstream", "--", "origin", "HEAD"]
    await action(delivery.GitDeliveryRequest(action="push", remote="origin", expected_branch="feature"))
    assert "--force-with-lease" not in commands[-1]
    for action_name in ("draft_pr" if saved.create_draft_pull_requests else "create_pr", "draft_pr"):
        result = await action(delivery.GitDeliveryRequest(action=action_name, expected_branch="feature", title="Explicit title", base="main", body="Explicit body"))
        assert ("--draft" in commands[-1]) is (action_name == "draft_pr")
        assert result["url"] == "https://github.com/org/repo/pull/1"


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["local", "remote"])
async def test_git_generation_uses_scoped_instructions_and_original_executor_without_mutations(tmp_path, monkeypatch, target):
    from backend import config
    from backend.llm import model_registry
    from backend.workspace import executor_commands

    routes_git_settings.put_git_settings(settings.GitPreferences(commit_instructions="COMMIT_ONLY", pull_request_instructions="PR_ONLY"), Response())
    commands = []
    adapter = SimpleNamespace(side_query=AsyncMock(side_effect=[json.dumps({"message": "Keep staged changes"}),
        json.dumps({"title": "Keep reviewed changes", "body": "## Summary\n- Reviewed diff\n\n## Testing\nNot run (not requested)"})]), aclose=AsyncMock())
    monkeypatch.setattr(model_registry, "create_session_llm", lambda config: adapter)
    monkeypatch.setattr(config, "load_config", lambda **kwargs: "local-config")

    def output(args):
        commands.append(args)
        if args[0] == "branch": return "feature"
        if args[0] == "rev-parse": return "a" * 40
        if "--name-only" in args: return "staged.py\0"
        return "diff --git a/staged.py b/staged.py\n@@ -1 +1 @@\n-old\n+new"

    registration = Mock()
    monkeypatch.setattr(executor_commands, "require_workspace_registration", registration)
    if target == "local":
        service = SimpleNamespace(workspace_root_path=lambda: tmp_path)

        async def command(root, argv, **kwargs):
            assert root == tmp_path
            return output(argv[1:]), 0, ""

        monkeypatch.setattr(generation, "run_delivery_command", command)
    else:
        service = Mock(spec=ExecutorEditorService)
        service.context = SimpleNamespace(turn_environment=SimpleNamespace(is_remote=True))
        service.application_config = "original-config"
        service.identity.return_value = {"environment_id": "original", "conversation_id": "owner"}

        async def command(owner, argv):
            assert owner is service
            return SimpleNamespace(returncode=0, stdout=output(argv[1:]).encode())

        monkeypatch.setattr(generation, "run_executor_git", command)
    for kind, marker, excluded in (("commit", "COMMIT_ONLY", "PR_ONLY"), ("pull_request", "PR_ONLY", "COMMIT_ONLY")):
        result = await generation.generate_git_message(service, generation.GitMessageRequest(kind=kind, message="Original draft", base="main", expected_branch="feature", expected_registration_id="accepted"))
        call = adapter.side_query.await_args
        assert marker in call.args[0][1].content and excluded not in call.args[0][1].content
        assert "staged.py" in call.args[0][1].content
        assert call.kwargs["options"].max_retries == 0
        assert call.kwargs["options"].hosted_web_search is False
        assert call.kwargs["options"].output_schema["additionalProperties"] is False
        if target == "remote": assert result["environment_id"] == "original"
    assert {args[0] for args in commands} == {"branch", "diff", "rev-parse"}
    assert adapter.aclose.await_count == 2
    with pytest.raises(HTTPException) as stale:
        await generation.generate_git_message(service, generation.GitMessageRequest(kind="commit", expected_branch="other"))
    assert stale.value.status_code == 409 and adapter.side_query.await_count == 2
    if target == "remote": registration.assert_any_call(service, "accepted")
