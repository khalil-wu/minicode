from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace

import pytest

import backend.agent.runtime as runtime_module
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.swarm_store import SubagentResourceOwnershipError
from backend.agent.worktree import create_agent_worktree
from backend.artifact.store import ArtifactStore
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.agent_tools import TaskTool


def _git(repository: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repository), *args],
        capture_output=True, text=True, encoding="utf-8", check=False)


@pytest.fixture
def continuation_owner(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    assert _git(repository, "init", "-q").returncode == 0
    assert _git(repository, "-c", "user.name=Continuation Audit", "-c",
        "user.email=audit@example.invalid", "commit", "-q", "--allow-empty",
        "-m", "owned continuation fixture").returncode == 0
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl",
        swarm_store_dir=tmp_path / "swarm", enable_lease_heartbeat=False)

    def held(name: str, *, session_id: str = "owner-session", legacy_owner: bool = False):
        from backend.services.git_settings_service import GitPreferences, update_git_preferences

        conversation_id = "conversation-" + name
        parent = runtime.start_run(run_id="parent-" + name,
            conversation_id=conversation_id, session_id=session_id)
        child = runtime.start_subagent(subagent_id="subagent-" + name,
            parent_run_id=parent.run_id, agent_type="worker", session_id=session_id,
            background=True, resume_config={"isolation": "worktree"})
        if legacy_owner:
            update_git_preferences(GitPreferences(branch_prefix="minicode/"))
        worktree, error = create_agent_worktree(child.subagent_id, repository)
        assert worktree is not None, error
        metadata = {"git_root": str(worktree.git_root), "branch": worktree.branch, "head_commit": worktree.head_commit}
        resume = {"isolation": "worktree", "worktree_path": str(worktree.worktree_path), "worktree_head_commit": worktree.head_commit}
        if not legacy_owner:
            metadata["worktree_branch"] = resume["worktree_branch"] = worktree.temporary_branch
        assert runtime.register_subagent_cleanup_resource(child.subagent_id,
            resource_kind="worktree", resource_id=str(worktree.worktree_path),
            metadata=metadata)
        assert runtime.update_subagent_resume_config(child.subagent_id,
            resume,
            agent_path=child.agent_path, mailbox_epoch=child.mailbox_epoch) is not None
        if legacy_owner:
            creation = runtime.start_run(conversation_id=conversation_id, parent_run_id=parent.run_id,
                role="subagent:worker", task_id=child.subagent_id, session_id=session_id, mailbox_epoch=child.mailbox_epoch)
            runtime.execution_journal(child.subagent_id).append("user_prompt", {
                "run_id": creation.run_id, "worktree_path": str(worktree.worktree_path),
                "worktree_branch": worktree.branch, "content": "Original admitted worktree request",
            })
            runtime.commit_terminal(creation.run_id, "completed")
            update_git_preferences(GitPreferences(branch_prefix="after-legacy/"))
        mail = runtime.send_swarm_message(sender_id=parent.run_id,
            recipient_id=child.subagent_id, content="Accepted work must survive retirement",
            conversation_id=conversation_id, delivery_mode="queue_only")
        runtime.close_subagent_mailbox(child.subagent_id,
            agent_path=child.agent_path, mailbox_epoch=child.mailbox_epoch)
        assert runtime.begin_subagent_worktree_retirement(child.subagent_id,
            agent_path=child.agent_path, mailbox_epoch=child.mailbox_epoch,
            resource_id=str(worktree.worktree_path))
        assert runtime.complete_subagent(child.subagent_id,
            agent_path=child.agent_path, mailbox_epoch=child.mailbox_epoch) is not None
        record = runtime.get_subagent(child.subagent_id)
        assert record.cleanup_resources[0]["state"] == "continuation"
        assert not record.cleanup_pending and not runtime.has_live_subagent_task(child.subagent_id)
        return SimpleNamespace(parent=parent, child=record, worktree=worktree,
            mail=mail, conversation_id=conversation_id, session_id=session_id)

    yield SimpleNamespace(runtime=runtime, repository=repository, held=held, root=tmp_path)
    runtime.close(release_lease=True)


def _assert_worktree_retirement(owner, item, *, changed: bool):
    assert item.worktree.worktree_path.exists() is changed
    branch = _git(owner.repository, "show-ref", "--verify", "--quiet",
        "refs/heads/" + item.worktree.branch)
    assert (branch.returncode == 0) is changed
    if changed:
        assert (item.worktree.worktree_path / "user-draft.txt").read_text(
            encoding="utf-8") == "unsaved user work stays here\n"
    assert owner.runtime._swarm_store.get_subagent(item.child.subagent_id) is None
    assert owner.runtime._swarm_store.list_messages(
        conversation_id=item.conversation_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True], ids=["clean", "user-edits"])
async def test_live_conversation_delete_retires_its_held_workspace_only(
    continuation_owner, changed,
):
    owner = continuation_owner
    item, peer = owner.held("deleted"), owner.held("peer", session_id="peer-session")
    if changed:
        (item.worktree.worktree_path / "user-draft.txt").write_text(
            "unsaved user work stays here\n", encoding="utf-8")
    peer_before = owner.runtime._swarm_store.get_subagent(peer.child.subagent_id)

    assert await owner.runtime.stop_subagent_tasks_for_conversation(item.conversation_id)
    removed = owner.runtime.purge_conversation(item.conversation_id)

    assert removed["subagent_ids"] == [item.child.subagent_id]
    assert not removed.get("cleanup_pending")
    _assert_worktree_retirement(owner, item, changed=changed)
    assert peer.worktree.worktree_path.is_dir()
    assert owner.runtime._swarm_store.get_subagent(peer.child.subagent_id) == peer_before


@pytest.mark.parametrize("changed", [False, True], ids=["clean", "user-edits"])
def test_cold_conversation_delete_finishes_original_workspace_without_global_hydration(
    continuation_owner, monkeypatch, changed,
):
    owner = continuation_owner
    item, peer = owner.held("deleted", legacy_owner=True), owner.held("peer", session_id="peer-session")
    worktree = item.worktree.worktree_path
    assert _git(worktree, "switch", "-c", "preserved-user-branch").returncode == 0
    (worktree / "user-commit.txt").write_text("committed user work survives\n", encoding="utf-8")
    assert _git(worktree, "add", "user-commit.txt").returncode == 0
    assert _git(worktree, "-c", "user.name=Owner Audit", "-c", "user.email=audit@example.invalid",
        "commit", "-m", "User branch commit").returncode == 0
    current_head = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    legacy = owner.runtime.get_subagent(item.child.subagent_id).to_dict()
    legacy["cleanup_resources"][0]["metadata"].update(branch="preserved-user-branch", head_commit=current_head)
    legacy["resume_config"]["worktree_head_commit"] = current_head
    assert "worktree_branch" not in legacy["cleanup_resources"][0]["metadata"]
    assert owner.runtime._swarm_store.upsert_subagent(legacy,
        expected_owner_token=legacy["runtime_owner_token"]) is not None
    from backend.agent.worktree_owner_upgrade import WorktreeOwnerUpgradeError, upgrade_legacy_worktree_owners
    with pytest.raises(WorktreeOwnerUpgradeError, match="original registration and admitted prompt"):
        upgrade_legacy_worktree_owners(legacy, journal_root=owner.root / "missing-creation-journal", read_run=owner.runtime._swarm_store.get_agent_run,
            states={"active", "retiring", "continuation"})
    assert worktree.exists()
    if changed:
        (item.worktree.worktree_path / "user-draft.txt").write_text(
            "unsaved user work stays here\n", encoding="utf-8")
    peer_before = owner.runtime._swarm_store.get_subagent(peer.child.subagent_id)
    owner.runtime.close(release_lease=True)
    monkeypatch.setattr(runtime_module, "_DEFAULT_RUNTIME", None)
    monkeypatch.setattr(runtime_module, "SWARM_DIR", owner.root / "swarm")

    def unexpected_hydration(*args, **kwargs):
        raise AssertionError("targeted conversation deletion must not hydrate the global runtime")

    monkeypatch.setattr(runtime_module, "AgentRuntime", unexpected_hydration)
    retire = runtime_module._purge_conversation_files

    def verify_upgrade(*args, **kwargs):
        persisted = owner.runtime._swarm_store.get_subagent(item.child.subagent_id)
        metadata = persisted["cleanup_resources"][0]["metadata"]
        assert metadata["worktree_branch"] == item.worktree.temporary_branch
        assert metadata["branch"] == "preserved-user-branch" and metadata["head_commit"] == current_head
        assert metadata["worktree_owner_upgrade"]["agent_path"] == item.child.agent_path
        assert persisted["resume_config"]["worktree_branch"] == item.worktree.temporary_branch
        return retire(*args, **kwargs)

    monkeypatch.setattr(runtime_module, "_purge_conversation_files", verify_upgrade)
    removed = runtime_module.purge_persisted_conversation_runtime(item.conversation_id)

    assert removed["subagent_ids"] == [item.child.subagent_id]
    assert not removed.get("cleanup_pending")
    _assert_worktree_retirement(owner, item, changed=changed)
    assert runtime_module.default_runtime_if_initialized() is None
    assert peer.worktree.worktree_path.is_dir()
    assert owner.runtime._swarm_store.get_subagent(peer.child.subagent_id) == peer_before
    assert _git(owner.repository, "show", "preserved-user-branch:user-commit.txt").stdout == "committed user work survives\n"


@pytest.mark.asyncio
async def test_session_shutdown_stops_only_its_idle_scheduler_and_preserves_accepted_mail(
    continuation_owner, monkeypatch,
):
    owner = continuation_owner
    first, peer = owner.held("old-session"), owner.held("peer", session_id="peer-session")
    runtime = owner.runtime
    tool = TaskTool(artifact_store=ArtifactStore(storage_dir=owner.root / "artifacts"))
    invoked = []

    async def unexpected_resume(**kwargs):
        invoked.append(kwargs["subagent_id"])
        raise AssertionError("the retired session's scheduler must not enter a model turn")

    monkeypatch.setattr(tool, "resume_background_subtask", unexpected_resume)
    barriers, schedulers = {}, {}
    for item in (first, peer):
        context = ToolExecutionContext(permission=PermissionContext(mode="bypass"),
            workspace_root=owner.repository, session_id=item.session_id,
            conversation_id=item.conversation_id, metadata={"run_id": item.parent.run_id},
            run_context=RunContext(agent_runtime=runtime))
        tool.bind_mailbox_resumer(item.child.subagent_id, context)
        finished, started = asyncio.get_running_loop().create_future(), asyncio.get_running_loop().create_future()
        started.set_result(None)
        barriers[item.child.subagent_id] = finished
        runtime.register_subagent_execution(item.child.subagent_id, finished, started)
        runtime.start_subagent_mailbox_turn(item.child.subagent_id)
        schedulers[item.child.subagent_id] = runtime._subagent_mailbox_turns[item.child.subagent_id][0]
    await asyncio.sleep(0)
    before = runtime._swarm_store.get_subagent(first.child.subagent_id)
    mail_before = runtime._swarm_store.list_messages(conversation_id=first.conversation_id)
    try:
        assert await runtime.stop_subagent_tasks_for_session(first.session_id)
        assert schedulers[first.child.subagent_id].done()
        assert not barriers[first.child.subagent_id].cancelled()
        assert not schedulers[peer.child.subagent_id].done()
        assert first.child.subagent_id not in runtime._subagent_mailbox_resumers
        barriers[first.child.subagent_id].set_result(None)
        await asyncio.sleep(0)
        assert invoked == []
        assert runtime._swarm_store.get_subagent(first.child.subagent_id) == before
        assert runtime._swarm_store.list_messages(conversation_id=first.conversation_id) == mail_before
        assert first.worktree.worktree_path.is_dir() and peer.worktree.worktree_path.is_dir()
    finally:
        runtime.close(release_lease=True)
        await asyncio.gather(*schedulers.values(), return_exceptions=True)


@pytest.mark.asyncio
async def test_purge_callback_keeps_other_recipient_writes_available(continuation_owner):
    owner = continuation_owner
    item, peer = owner.held("deleted"), owner.held("peer", session_id="peer-session")
    entered, release = threading.Event(), threading.Event()

    def cleanup(durable):
        entered.set()
        assert release.wait(timeout=10)
        return runtime_module._purge_conversation_files(
            set(durable["run_ids"]) | set(durable["subagent_ids"]),
            conversation_id=item.conversation_id, journal_root=owner.runtime._journal_root,
            outbox_root=owner.runtime._outbox_root, subagent_records=durable["subagent_records"])

    purging = asyncio.create_task(asyncio.to_thread(owner.runtime._swarm_store.purge_conversation,
        item.conversation_id, allowed_active_owner_tokens={owner.runtime._runtime_owner_token},
        before_delete=cleanup))
    sending = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        sending = asyncio.create_task(asyncio.to_thread(owner.runtime.send_swarm_message,
            sender_id=peer.parent.run_id, recipient_id=peer.child.subagent_id,
            conversation_id=peer.conversation_id, content="Peer mailbox remains available",
            delivery_mode="queue_only"))
        done, _ = await asyncio.wait({sending}, timeout=3)
        assert sending in done, "physical Git cleanup must not hold SQLite's global writer"
        message = sending.result()
        assert message.content == "Peer mailbox remains available"
        assert not purging.done()
    finally:
        release.set()
        await asyncio.gather(purging, *([sending] if sending is not None else []), return_exceptions=True)
    assert purging.result()["subagent_ids"] == [item.child.subagent_id]
    _assert_worktree_retirement(owner, item, changed=False)
    assert peer.worktree.worktree_path.is_dir()


@pytest.mark.asyncio
async def test_purge_does_not_delete_a_new_conversation_owner_after_cleanup(continuation_owner):
    owner = continuation_owner
    item = owner.held("deleted")
    entered, release = threading.Event(), threading.Event()

    def cleanup(durable):
        entered.set()
        assert release.wait(timeout=10)
        return runtime_module._purge_conversation_files(
            set(durable["run_ids"]) | set(durable["subagent_ids"]),
            conversation_id=item.conversation_id, journal_root=owner.runtime._journal_root,
            outbox_root=owner.runtime._outbox_root, subagent_records=durable["subagent_records"])

    purging = asyncio.create_task(asyncio.to_thread(owner.runtime._swarm_store.purge_conversation,
        item.conversation_id, allowed_active_owner_tokens={owner.runtime._runtime_owner_token},
        before_delete=cleanup))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        new_parent = await asyncio.to_thread(owner.runtime.start_run,
            run_id="new-conversation-owner", conversation_id=item.conversation_id,
            session_id="new-session")
        new_child = await asyncio.to_thread(owner.runtime.start_subagent,
            subagent_id="subagent-new-owner", parent_run_id=new_parent.run_id,
            agent_type="worker", session_id="new-session", background=True)
    finally:
        release.set()
    with pytest.raises(SubagentResourceOwnershipError, match="ownership changed"):
        await purging
    assert owner.runtime._swarm_store.get_agent_run(new_parent.run_id) is not None
    assert owner.runtime._swarm_store.get_subagent(new_child.subagent_id) is not None
    original = owner.runtime._swarm_store.get_subagent(item.child.subagent_id)
    assert original is not None and original["cleanup_pending"]
    assert original["cleanup_resources"][0]["state"] == "retiring"
    assert owner.runtime._swarm_store.list_messages(conversation_id=item.conversation_id)
