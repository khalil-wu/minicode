from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from backend.agent.message import AgentEvent
from backend.services import scheduled_task_runner as runner


class _Repository:
    def __init__(self, conversation=None) -> None:
        self.conversation = conversation
        self.created = []
        self.messages = []
        self.snapshots = []

    def get_conversation(self, conversation_id: str):
        return self.conversation if self.conversation and self.conversation.id == conversation_id else None

    def create_conversation(self, **kwargs):
        conversation = SimpleNamespace(
            id="conv_scheduled",
            archived=False,
            workspace_root=kwargs["workspace_root"],
            worktree_path="",
            git_isolated=False,
        )
        self.conversation = conversation
        self.created.append(kwargs)
        return conversation

    def update_workspace_binding(self, conversation_id: str, **kwargs):
        if not self.conversation or self.conversation.id != conversation_id:
            return None
        for key, value in kwargs.items():
            setattr(self.conversation, key, value)
        return self.conversation

    def append_transcript_message(self, conversation_id: str, message: dict):
        self.messages.append((conversation_id, message))

    def save_context_snapshot(self, conversation_id: str, snapshot: dict):
        self.snapshots.append((conversation_id, snapshot))


def _task(workspace: Path, **overrides):
    values = {
        "id": "task_1",
        "name": "Nightly",
        "prompt": "Run the checks",
        "workspace_root": str(workspace),
        "conversation_id": "",
        "permission_mode": "auto_approve",
        "isolation": "workspace",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _bootstrap():
    return SimpleNamespace(config=SimpleNamespace(agent=SimpleNamespace(max_iterations=5)))


def test_scheduled_runner_rejects_missing_workspace(tmp_path) -> None:
    result = asyncio.run(runner.run_scheduled_task(_task(tmp_path / "missing"), SimpleNamespace(id="run_1"), bootstrap=_bootstrap()))

    assert result["status"] == "failed"
    assert "workspace is unavailable" in result["error"]


def test_scheduled_heartbeat_fails_closed_when_conversation_is_missing(monkeypatch, tmp_path) -> None:
    repository = _Repository()
    monkeypatch.setattr(runner, "ConversationRepository", lambda: repository)
    monkeypatch.setattr(runner, "main_worktree_root", lambda path: Path(path).resolve())

    result = asyncio.run(runner.run_scheduled_task(
        _task(tmp_path, conversation_id="conv_missing"),
        SimpleNamespace(id="run_2"),
        bootstrap=_bootstrap(),
    ))

    assert result["status"] == "failed"
    assert result["conversation_id"] == "conv_missing"
    assert repository.created == []


def test_scheduled_worktree_creation_is_fail_closed(monkeypatch, tmp_path) -> None:
    repository = _Repository()
    monkeypatch.setattr(runner, "ConversationRepository", lambda: repository)
    monkeypatch.setattr(runner, "main_worktree_root", lambda _path: tmp_path)
    monkeypatch.setattr(runner, "git_branch_for", lambda _path: "main")

    from backend.services import conversation_payload_service

    monkeypatch.setattr(
        conversation_payload_service,
        "create_isolated_worktree_binding",
        lambda *_args, **_kwargs: SimpleNamespace(
            created=False,
            error_event=AgentEvent.error("worktree unavailable"),
        ),
    )

    result = asyncio.run(runner.run_scheduled_task(
        _task(tmp_path, isolation="worktree"),
        SimpleNamespace(id="run_3"),
        bootstrap=_bootstrap(),
    ))

    assert result["status"] == "failed"
    assert result["error"] == "worktree unavailable"


def test_scheduled_runner_executes_in_managed_worktree(monkeypatch, tmp_path) -> None:
    repository = _Repository()
    worktree = tmp_path / ".minicode" / "worktrees" / "conv_scheduled"
    worktree.mkdir(parents=True)
    monkeypatch.setattr(runner, "ConversationRepository", lambda: repository)
    monkeypatch.setattr(runner, "main_worktree_root", lambda _path: tmp_path)
    monkeypatch.setattr(runner, "git_branch_for", lambda _path: "main")

    from backend.services import conversation_payload_service

    monkeypatch.setattr(
        conversation_payload_service,
        "create_isolated_worktree_binding",
        lambda *_args, **_kwargs: SimpleNamespace(
            created=True,
            workspace_root=str(worktree),
            git_branch="minicode/conv_scheduled",
            worktree_path=str(worktree),
            error_event=None,
        ),
    )
    observed = {}

    async def fake_run_rest_chat(**kwargs):
        observed.update(kwargs)
        return {"stopped_reason": "completed", "reply": "All checks passed", "iterations": 2}

    monkeypatch.setattr(runner, "run_owned_rest_chat", fake_run_rest_chat)

    result = asyncio.run(runner.run_scheduled_task(
        _task(tmp_path, isolation="worktree"),
        SimpleNamespace(id="run_4"),
        bootstrap=_bootstrap(),
    ))

    assert result["status"] == "completed"
    assert result["execution_workspace_root"] == str(worktree.resolve())
    assert observed["workspace_root"] == worktree.resolve()
    assert observed["conversation_id"] == "conv_scheduled"
    assert [message[1]["role"] for message in repository.messages] == ["user", "assistant"]


def test_scheduled_runner_persists_partial_status_reason_and_errors(monkeypatch, tmp_path) -> None:
    repository = _Repository()
    monkeypatch.setattr(runner, "ConversationRepository", lambda: repository)
    monkeypatch.setattr(runner, "main_worktree_root", lambda _path: tmp_path)

    async def fake_run_rest_chat(**_kwargs):
        return {
            "status": "partial",
            "stopped_reason": "max_iterations",
            "reply": "Completed the inspection but not the migration.",
            "errors": ["Provider retry exhausted after evidence was collected."],
            "iterations": 5,
        }

    monkeypatch.setattr(runner, "run_owned_rest_chat", fake_run_rest_chat)

    result = asyncio.run(runner.run_scheduled_task(
        _task(tmp_path, isolation="workspace"),
        SimpleNamespace(id="run_partial"),
        bootstrap=_bootstrap(),
    ))

    assert result["status"] == "partial"
    assert result["error"] == ""
    assistant = repository.messages[-1][1]
    assert assistant["role"] == "assistant"
    assert assistant["terminal_status"] == "partial"
    assert assistant["termination_reason"] == "max_iterations"
    assert assistant["metadata"]["errors"] == [
        "Provider retry exhausted after evidence was collected."
    ]
    assert "failure_message" not in assistant


def test_scheduled_runner_rejects_busy_conversation_before_side_effects(monkeypatch, tmp_path) -> None:
    from backend.agent.conversation_query_guard import conversation_query_guards

    conversation = SimpleNamespace(
        id="conv_busy",
        archived=False,
        workspace_root=str(tmp_path),
        worktree_path="",
        git_isolated=False,
    )
    repository = _Repository(conversation)
    monkeypatch.setattr(runner, "ConversationRepository", lambda: repository)
    monkeypatch.setattr(runner, "main_worktree_root", lambda path: Path(path).resolve())
    query_guards = conversation_query_guards()
    active = query_guards.try_start("conv_busy", owner_id="ws:active")
    assert active is not None
    try:
        result = asyncio.run(runner.run_scheduled_task(
            _task(tmp_path, conversation_id="conv_busy", isolation="worktree"),
            SimpleNamespace(id="run_busy"),
            bootstrap=_bootstrap(),
        ))
    finally:
        query_guards.end(active)

    assert result["status"] == "failed"
    assert "active turn" in result["error"]
    assert repository.messages == []
    assert repository.snapshots == []
