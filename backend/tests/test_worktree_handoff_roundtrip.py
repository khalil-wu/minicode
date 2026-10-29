from __future__ import annotations

from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.conversations.repository import ConversationRepository
from backend.services import conversation_payload_service as payload
from backend.services import conversation_worktree_handoff_service as handoff
from backend.services.worktree_settings_service import WorktreePreferences, update_worktree_preferences
from backend.services.workspace_service import main_worktree_root
from backend.workspace.trust import trust_created_task_directory
from backend.ws.handlers import conversation as handlers


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
        text=True, encoding="utf-8").stdout.strip()


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "offline@example.invalid")
    git(repo, "config", "user.name", "Worktree fixture")
    (repo / "tracked.txt").write_text("original\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Initial fixture")
    trust_created_task_directory(repo)
    update_worktree_preferences(WorktreePreferences(worktree_root=str(tmp_path / "pool")))
    repository = ConversationRepository(tmp_path / "conversations")
    conversation = repository.create_conversation(conversation_id="conv-roundtrip", workspace_root=str(repo), git_branch="main")
    session = SimpleNamespace(session_id="handoff-test", active_conversation_id="reader-other",
        conversation_repo=repository, main_worktree_root=main_worktree_root,
        attachment_store=SimpleNamespace(share_for_conversation=Mock()),
        artifact_store=SimpleNamespace(share_for_conversation=Mock()),
        running_agent_task_for=lambda _owner: None, ws_manager=None,
        emit_command_result=AsyncMock())
    project = AsyncMock(return_value=[])
    release = AsyncMock(return_value=[])
    monkeypatch.setattr(handlers, "_switch_active_sessions_to_conversation_workspace", project)
    monkeypatch.setattr(handlers, "_release_active_sessions_from_conversation_workspace", release)
    monkeypatch.setattr(handlers, "_broadcast_conversation_lists", AsyncMock(return_value=[]))
    monkeypatch.setattr(handlers, "_conversation_activity_blockers", lambda *_args: {})

    async def preflight(target, **options):
        await handlers.handle_conversation_worktree_handoff_preflight(session,
            {"conversation_id": conversation.id, "target": target, **options})
        return session.emit_command_result.await_args.kwargs["data"]

    async def execute(preflight):
        await handlers.handle_conversation_worktree_handoff_execute(session, {
            "conversation_id": conversation.id, "target": preflight["target"],
            "dirty_action": preflight["dirty_action"], "fingerprint": preflight["fingerprint"],
            "local_branch": preflight["local_checkout_branch"],
        })
        return session.emit_command_result.await_args.kwargs["data"]

    async def first_roundtrip():
        initial = await preflight("worktree")
        assert initial["allowed"], initial
        assert not initial["reuse_branch"]
        created = await execute(initial)
        assert created["completed"], created
        worktree = Path(created["worktree_path"])
        (worktree / "task.txt").write_text("task commit\n", encoding="utf-8")
        git(worktree, "add", "task.txt")
        git(worktree, "commit", "-m", "Task work")
        returning = await preflight("local")
        assert returning["allowed"], returning
        returned = await execute(returning)
        assert returned["completed"], returned
        assert not worktree.exists()
        assert git(repo, "branch", "--show-current") == created["git_branch"]
        return worktree, created["git_branch"], git(repo, "rev-parse", "HEAD")

    return SimpleNamespace(repo=repo, repository=repository, conversation=conversation, session=session,
        project=project, release=release, preflight=preflight, execute=execute, first_roundtrip=first_roundtrip)


@pytest.mark.asyncio
async def test_same_task_roundtrips_reuse_the_committed_branch_and_project_durable_binding(scenario):
    s = scenario
    worktree, branch, commit = await s.first_roundtrip()
    for _ in range(2):
        preflight = await s.preflight("worktree")
        assert preflight["allowed"] and preflight["reuse_branch"], preflight
        assert preflight["local_branches"] == ["main"]
        assert preflight["local_checkout_branch"] == "main"
        result = await s.execute(preflight)
        assert result["completed"] and result["git_branch"] == branch
        assert git(s.repo, "branch", "--show-current") == "main"
        assert git(worktree, "branch", "--show-current") == branch
        assert git(worktree, "rev-parse", "HEAD") == commit
        assert (worktree / "task.txt").read_text(encoding="utf-8") == "task commit\n"
        assert git(s.repo, "for-each-ref", "--format=%(refname:short)", "refs/heads") == f"{branch}\nmain"
        bound = s.repository.get_conversation(s.conversation.id)
        assert bound.git_isolated and Path(bound.workspace_root) == worktree
        assert s.project.await_args.args[1].workspace_root == str(worktree)
        returning = await s.preflight("local")
        assert returning["allowed"], returning
        assert (await s.execute(returning))["completed"]
        assert not worktree.exists()
        assert git(s.repo, "rev-parse", "HEAD") == commit
    assert s.session.active_conversation_id == "reader-other"
    assert s.session.attachment_store.share_for_conversation.call_count == 6


@pytest.mark.asyncio
async def test_unrelated_same_name_branch_stays_a_collision_without_extra_branch_inventory(scenario, monkeypatch):
    s = scenario
    git(s.repo, "branch", "codex/conv-roundtrip")
    reads = Mock(wraps=handoff._git_snapshot)
    monkeypatch.setattr(handoff, "_git_snapshot", reads)
    preflight = await s.preflight("worktree")
    assert not preflight["allowed"] and not preflight["reuse_branch"]
    assert any(check["code"] == "branch.collision" for check in preflight["checks"])
    assert not any(call.args[1] == "for-each-ref" for call in reads.call_args_list)


@pytest.mark.asyncio
async def test_explicit_local_branch_and_stash_preserve_staging_and_untracked_files(scenario):
    s = scenario
    worktree, branch, _ = await s.first_roundtrip()
    git(s.repo, "branch", "other-local", "main")
    (s.repo / "tracked.txt").write_text("staged task edit\n", encoding="utf-8")
    git(s.repo, "add", "tracked.txt")
    (s.repo / "untracked.txt").write_text("local draft\n", encoding="utf-8")
    blocked = await s.preflight("worktree")
    assert not blocked["allowed"]
    preflight = await s.preflight("worktree", dirty_action="stash", local_branch="other-local")
    assert preflight["allowed"] and preflight["local_checkout_branch"] == "other-local", preflight
    result = await s.execute(preflight)
    assert result["completed"]
    assert result["git_branch"] == branch and result["stash_ref"]
    assert git(s.repo, "branch", "--show-current") == "other-local"
    assert not git(s.repo, "status", "--porcelain")
    assert git(worktree, "status", "--porcelain").splitlines() == ["M  tracked.txt", "?? untracked.txt"]
    assert (worktree / "untracked.txt").read_text(encoding="utf-8") == "local draft\n"


@pytest.mark.asyncio
async def test_local_candidate_head_change_invalidates_execute_without_switching_or_creating(scenario):
    s = scenario
    worktree, branch, commit = await s.first_roundtrip()
    git(s.repo, "branch", "other-local", "main")
    preflight = await s.preflight("worktree", local_branch="other-local")
    assert preflight["allowed"]
    git(s.repo, "update-ref", "refs/heads/other-local", commit)
    result = await s.execute(preflight)
    assert result["stale"] and not result.get("completed")
    assert not worktree.exists()
    assert git(s.repo, "branch", "--show-current") == branch
    assert not s.repository.get_conversation(s.conversation.id).git_isolated


@pytest.mark.asyncio
async def test_reuse_binding_failure_restores_original_branch_and_stash_without_deleting_ref(scenario, monkeypatch):
    s = scenario
    worktree, branch, commit = await s.first_roundtrip()
    (s.repo / "tracked.txt").write_text("staged draft\n", encoding="utf-8")
    git(s.repo, "add", "tracked.txt")
    (s.repo / "untracked.txt").write_text("unsaved draft\n", encoding="utf-8")
    preflight = await s.preflight("worktree", dirty_action="stash")
    assert preflight["allowed"]
    monkeypatch.setattr(s.repository, "update_workspace_binding", Mock(return_value=None))
    delete_branch = Mock(side_effect=AssertionError("A reused task branch must survive rollback"))
    monkeypatch.setattr(handoff, "delete_local_branch", delete_branch)
    result = await s.execute(preflight)
    assert result["reason"] == "workspace_binding_failed"
    assert result["rollback_completed"] and not result["recovery_required"], result
    assert not worktree.exists()
    assert git(s.repo, "branch", "--show-current") == branch
    assert git(s.repo, "rev-parse", "HEAD") == commit
    assert git(s.repo, "status", "--porcelain").splitlines() == ["M  tracked.txt", "?? untracked.txt"]
    assert not s.repository.get_conversation(s.conversation.id).git_isolated
    delete_branch.assert_not_called()


@pytest.mark.asyncio
async def test_reuse_creation_failure_restores_checkout_before_applying_source_stash(scenario, monkeypatch):
    s = scenario
    worktree, branch, commit = await s.first_roundtrip()
    (s.repo / "tracked.txt").write_text("kept draft\n", encoding="utf-8")
    preflight = await s.preflight("worktree", dirty_action="stash")
    create = Mock(return_value=payload.IsolatedWorktreeCreationResult(created=False, conversation_id=s.conversation.id))
    monkeypatch.setattr(payload, "create_isolated_worktree_binding", create)
    result = await s.execute(preflight)
    assert result["reason"] == "worktree_creation_failed" and result["rollback_completed"], result
    assert not worktree.exists()
    assert git(s.repo, "branch", "--show-current") == branch
    assert git(s.repo, "rev-parse", "HEAD") == commit
    assert (s.repo / "tracked.txt").read_text(encoding="utf-8") == "kept draft\n"
    assert create.call_args.kwargs["new_branch"] is False
    assert create.call_args.kwargs["branch"] == branch


@pytest.mark.asyncio
async def test_local_checkout_failure_keeps_binding_and_restores_stash_before_retry(scenario, monkeypatch):
    s = scenario
    worktree, branch, _ = await s.first_roundtrip()
    (s.repo / "tracked.txt").write_text("kept draft\n", encoding="utf-8")
    preflight = await s.preflight("worktree", dirty_action="stash")
    monkeypatch.setattr(handoff, "switch_main_checkout", Mock(return_value=(False, "checkout is locked")))
    create = Mock(side_effect=AssertionError("Do not attach before releasing the source branch"))
    monkeypatch.setattr(payload, "create_isolated_worktree_binding", create)
    result = await s.execute(preflight)
    assert result["reason"] == "local_checkout_failed" and result["rollback_completed"], result
    assert not worktree.exists() and not s.repository.get_conversation(s.conversation.id).git_isolated
    assert git(s.repo, "branch", "--show-current") == branch
    assert (s.repo / "tracked.txt").read_text(encoding="utf-8") == "kept draft\n"
    create.assert_not_called()


@pytest.mark.asyncio
async def test_reused_branch_keeps_running_shared_and_checked_out_candidate_blockers(scenario, tmp_path):
    s = scenario
    worktree, branch, _ = await s.first_roundtrip()
    s.session.running_agent_task_for = lambda _owner: object()
    running = await s.preflight("worktree")
    assert not running["allowed"] and any(check["code"] == "turn.running" for check in running["checks"])
    s.session.running_agent_task_for = lambda _owner: None
    other = s.repository.create_conversation(workspace_root=str(s.repo), git_branch=branch)
    shared = await s.preflight("worktree")
    assert not shared["allowed"] and any(check["code"] == "target.shared" for check in shared["checks"])
    s.repository.set_archived(other.id, archived=True)
    git(s.repo, "worktree", "add", str(tmp_path / "user-main-checkout"), "main")
    occupied = await s.preflight("worktree")
    assert not occupied["allowed"] and not occupied["local_branches"]
    assert any(check["code"] == "local.branch_unavailable" for check in occupied["checks"])
    assert not worktree.exists() and git(s.repo, "branch", "--show-current") == branch
