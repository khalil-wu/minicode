from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from backend.conversations.repository import ConversationRepository
from backend.hooks.runtime import run_config_change_hook
from backend.sandbox.policy import SandboxPolicy
from backend.sandbox.runner import SandboxRunner, SandboxUnavailableError
from backend.services import workspace_service
from backend.subprocesses import communicate_bounded


@pytest.mark.parametrize("windows_wrapper", [False, True])
def test_interactive_argv_preserves_shell_metacharacters(tmp_path, monkeypatch, windows_wrapper):
    if windows_wrapper and sys.platform != "win32":
        pytest.skip("Windows command quoting requires PowerShell")
    arguments = ["spaces and 'quotes'", 'literal"quote', "a&b|c", "$(Get-Date)", "x;y", "尾部\\"]

    async def scenario():
        private_temp = (tmp_path / "private-temp").resolve()
        assert private_temp.is_relative_to(tmp_path.resolve())
        private_temp.mkdir()
        policy = replace(SandboxPolicy.bypass(), env_overrides={"TEMP": str(private_temp), "TMP": str(private_temp)})
        runner = SandboxRunner(policy)
        if windows_wrapper:
            from backend.sandbox import windows_native

            def native_launch(command, **kwargs):
                assert kwargs["argv"] == [sys.executable, "-c", "import json,sys;print(json.dumps(sys.argv[1:]))", *arguments]
                return kwargs["argv"], None, None

            runner = SandboxRunner(SandboxPolicy(workspace_root=tmp_path, env_overrides=policy.env_overrides))
            monkeypatch.setattr(runner, "capability", lambda **_kwargs: SimpleNamespace(available=True, backend="windows-elevated-wfp"))
            monkeypatch.setattr(windows_native, "prepare_command", native_launch)
        process = await runner.spawn_interactive(
            [sys.executable, "-c", "import json,sys;print(json.dumps(sys.argv[1:]))", *arguments],
            cwd=tmp_path,
        )
        try:
            stdout, stderr = await communicate_bounded(
                process, timeout=5, stdout_limit_bytes=4096, stderr_limit_bytes=4096,
            )
            assert process.returncode == 0, stderr
            assert json.loads(stdout) == arguments
        finally:
            assert await runner.cleanup()

    asyncio.run(scenario())


def test_git_argv_preserves_literal_paths(tmp_path):
    from backend.tools.git_support import _run_git

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    path = "a&b '文档'.txt"
    (tmp_path / path).write_text("content", encoding="utf-8")
    result = asyncio.run(_run_git(
        ["git", "status", "--porcelain=v1", "-z", "--", path], root=tmp_path,
        sandbox_policy=SandboxPolicy.bypass(),
    ))
    assert result.returncode == 0, result.stderr
    assert result.stdout == ("?? " + path + "\0").encode()


def test_git_status_rejects_an_escaped_path_before_launch(tmp_path, monkeypatch):
    from backend.tools import git_tools

    async def launch(*_args, **_kwargs):
        pytest.fail("An escaped path must not dispatch Git in a different directory")

    monkeypatch.setattr(git_tools, "_run_git", launch)
    result = asyncio.run(git_tools.GitStatusTool(tmp_path).execute({"path": ".."}))
    assert result.is_error


@pytest.mark.parametrize("force", [False, True])
def test_worktree_status_failure_never_requests_force_or_deletes(tmp_path, monkeypatch, force):
    from backend.workspace.worktree import WorktreeManager

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    worktree = tmp_path / "checkout"
    worktree.mkdir()

    def unavailable(*_args, **_kwargs):
        raise SandboxUnavailableError("sandbox is offline")

    monkeypatch.setattr(workspace_service, "run_readonly_git", unavailable)
    removal = WorktreeManager(tmp_path).safe_remove_worktree(worktree, force=force)
    assert not removal.removed
    assert not removal.needs_force
    assert "sandbox is offline" in removal.error
    assert worktree.is_dir()


def test_worktree_preflight_detects_ignored_files(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("local.bin\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    (tmp_path / "local.bin").write_bytes(b"user data")
    monkeypatch.setattr(workspace_service, "readonly_git_policy", lambda root: replace(SandboxPolicy.bypass(), workspace_root=root))
    assert workspace_service.worktree_has_local_changes(tmp_path)


def test_config_hook_runtime_error_prevents_settings_mutation(tmp_path, monkeypatch):
    from backend.api.models import LLMSettingsUpdateRequest
    from backend.services import llm_settings_service

    class Hooks:
        async def run_config_change(self, **_kwargs):
            raise RuntimeError("hook runtime failed")

    writes = []
    monkeypatch.setattr("backend.hooks.get_hook_manager", lambda: Hooks())
    monkeypatch.setattr(llm_settings_service, "save_llm_settings", lambda value: writes.append(value))
    with pytest.raises(RuntimeError, match="hook runtime failed"):
        asyncio.run(llm_settings_service.update_llm_settings(
            LLMSettingsUpdateRequest(provider="openai", confirm_sensitive_change=True),
            settings_file=tmp_path / "settings.json", config_change_hook=run_config_change_hook,
        ))
    assert writes == []


def test_scheduled_worktree_cancellation_finishes_durable_binding(tmp_path, monkeypatch):
    from backend.services import conversation_payload_service, scheduled_task_runner

    entered, release = threading.Event(), threading.Event()
    destination = tmp_path / "scheduled-worktree"
    repository = ConversationRepository()
    conversation = repository.create_conversation(workspace_root=str(tmp_path))
    monkeypatch.setattr(scheduled_task_runner, "main_worktree_root", lambda _root: tmp_path)

    def create(*_args, **_kwargs):
        entered.set()
        assert release.wait(3)
        destination.mkdir()
        return conversation_payload_service.IsolatedWorktreeCreationResult(
            created=True, conversation_id=conversation.id, workspace_root=str(destination),
            worktree_path=str(destination), git_branch="fixture/scheduled",
        )

    monkeypatch.setattr(conversation_payload_service, "create_isolated_worktree_binding", create)

    async def scenario():
        operation = asyncio.create_task(scheduled_task_runner.run_scheduled_task(
            SimpleNamespace(workspace_root=str(tmp_path), isolation="worktree", permission_mode="confirm", conversation_id=conversation.id),
            SimpleNamespace(id="scheduled-cancel", conversation_id=conversation.id),
            bootstrap=SimpleNamespace(),
        ))
        try:
            async with asyncio.timeout(3):
                while not entered.is_set():
                    await asyncio.sleep(.001)
            operation.cancel()
            await asyncio.sleep(.01)
            assert not operation.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        saved = repository.get_conversation(conversation.id)
        assert saved.git_isolated
        assert saved.worktree_path == str(destination)
        assert destination.is_dir()

    asyncio.run(scenario())
