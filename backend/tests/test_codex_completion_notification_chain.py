from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.conversations.repository import ConversationRepository
from backend.conversations.public_projection import project_public_conversation
from backend.services.turn_notifications import completion_notification
from backend.tasks.scheduler import ScheduledTask, ScheduledTaskRun, TaskScheduler
from backend.services import scheduled_task_runner


@pytest.mark.parametrize("kind,status,text,policy,decision,body", [
    (None, "completed", "", None, None, "MiniCode 已完成一个回合。"),
    (None, "failed", "", None, None, "MiniCode 已完成一个回合。"),
    (None, "completed", "DONT_NOTIFY", None, None, "DONT_NOTIFY"),
    ("heartbeat", "completed", "", None, "DONT_NOTIFY", None),
    ("heartbeat", "failed", "", None, None, "MiniCode 已完成一个回合。"),
    ("heartbeat", "completed", "<![CDATA[ DONT_NOTIFY ]]>", None, "DONT_NOTIFY", None),
    ("cron", "completed", "Real result", "failed_runs_only", "DONT_NOTIFY", None),
    ("cron", "failed", "Real error", "failed_runs_only", "NOTIFY", "Real error"),
    (None, "completed", "<![CDATA[ :: SKIP_COMPLETION :: ]]>", None, None, None),
    (None, "failed", "::SKIP_COMPLETION::", None, None, "MiniCode 已完成一个回合。"),
])
def test_source_decision_contract_preserves_ordinary_empty_and_failed_turns(kind, status, text, policy, decision, body):
    notification = completion_notification(status=status, last_agent_message=text, automation_kind=kind, notification_policy=policy)
    assert notification["automation_notification_decision"] == decision
    assert notification["body"] == body
    assert notification["last_agent_message"] == text


def test_source_structured_decision_selects_last_envelope_and_preserves_visible_text():
    notification = completion_notification(status="completed", automation_kind="heartbeat", last_agent_message=(
        "Original result\n```xml\n<heartbeat><decision>DONT_NOTIFY</decision></heartbeat>\n```\n"
        "<heartbeat><decision>notify</decision><message><![CDATA[Actual notification]]></message></heartbeat>"))
    assert notification["body"] == "Actual notification"
    assert notification["heartbeat_assistant_message"]["visible_text"] == "Original result"
    assert notification["should_mark_unread"] is True


def test_source_normal_thread_xml_decision_is_control_data_without_changing_provenance():
    notification = completion_notification(status="completed", automation_kind=None, thread_source="user",
        last_agent_message="<heartbeat><decision>DONT_NOTIFY</decision><message>Explicit decision</message></heartbeat>")
    assert notification["automation_notification_decision"] == "DONT_NOTIFY"
    assert notification["body"] is None
    assert notification["should_mark_unread"] is False
    assert notification["automation_kind"] is None
    assert notification["thread_source"] == "user"


@pytest.mark.parametrize("source", ["aeon", "realtime_voice", "chatgpt_hidden", "dreaming"])
def test_explicit_source_provenance_controls_eligibility(source):
    notification = completion_notification(status="failed", last_agent_message="Real error", thread_source=source)
    assert notification["eligible"] is False
    assert notification["body"] == "Real error"
    assert notification["last_agent_message"] == "Real error"


def test_actual_continuation_controls_notification_independently_of_output():
    notification = completion_notification(status="completed", last_agent_message="Real final answer", has_pending_continuation=True)
    assert notification["eligible"] is False
    assert notification["should_mark_unread"] is True


def _terminal(repository, owner, message_id, notification):
    return repository.commit_turn_projection(owner.id, assistant_message={
        "id": message_id, "role": "assistant", "content": "Original visible answer", "terminal_status": "completed",
        "metadata": {"completion_notification": notification},
    }, context_snapshot={"history": [{"role": "assistant", "content": "Private actual provider context"}]})


def test_terminal_unread_is_atomic_durable_and_replay_does_not_undo_read(tmp_path):
    repository = ConversationRepository(base_dir=tmp_path / "data")
    owner = repository.create_conversation(title="Actual task", thread_source="user", workspace_root=str(tmp_path))
    notice = completion_notification(status="completed", last_agent_message="Original visible answer")
    committed = _terminal(repository, owner, "actual-message", notice)
    assert committed.has_unread_turn is True
    cold = ConversationRepository(base_dir=tmp_path / "data")
    restored = cold.get_conversation(owner.id)
    assert restored.has_unread_turn is True
    assert restored.transcript[-1]["metadata"]["completion_notification"]["body"] == "Original visible answer"
    assert project_public_conversation(restored)["thread_source"] == "user"
    cold.set_conversation_read_state(owner.id, False)
    _terminal(cold, owner, "actual-message", notice)
    assert ConversationRepository(base_dir=tmp_path / "data").get_conversation(owner.id).has_unread_turn is False


def test_quiet_heartbeat_keeps_preexisting_manual_unread_and_does_not_mark_a_read_thread(tmp_path):
    repository = ConversationRepository(base_dir=tmp_path / "data")
    notice = completion_notification(status="completed", automation_kind="heartbeat", last_agent_message="DONT_NOTIFY")
    read = repository.create_conversation(title="Read task", workspace_root=str(tmp_path))
    unread = repository.create_conversation(title="Manual unread task", workspace_root=str(tmp_path))
    repository.set_conversation_read_state(unread.id, True)
    assert _terminal(repository, read, "quiet-read", notice).has_unread_turn is False
    assert _terminal(repository, unread, "quiet-unread", notice).has_unread_turn is True


def test_partial_terminal_snapshot_does_not_consume_the_final_unread_commit(tmp_path):
    repository = ConversationRepository(base_dir=tmp_path / "data")
    owner = repository.create_conversation(title="Real stream boundary", workspace_root=str(tmp_path))
    repository.commit_turn_projection(owner.id, assistant_message={"id": "real-message", "role": "assistant", "content": "Original visible answer",
        "terminal_status": "completed"}, context_delta={"set": {}, "removed": []}, partial=True)
    assert repository.get_conversation(owner.id).has_unread_turn is False
    assert _terminal(repository, owner, "real-message", completion_notification(status="completed", last_agent_message="Original visible answer")).has_unread_turn is True


def test_real_scheduled_producer_persists_kind_policy_and_notification_with_terminal(monkeypatch, tmp_path):
    repository = ConversationRepository(base_dir=tmp_path / "data")
    owner = repository.create_conversation(title="Heartbeat owner", workspace_root=str(tmp_path), execution_cwd=str(tmp_path))
    monkeypatch.setattr(scheduled_task_runner, "ConversationRepository", lambda: repository)
    observed = []

    async def execute(**request):
        observed.append(request)
        return {"status": "completed", "stopped_reason": "completed", "reply": "DONT_NOTIFY", "errors": [], "iterations": 1, "context_snapshot": {}}

    monkeypatch.setattr(scheduled_task_runner, "run_owned_rest_chat", execute)
    task = ScheduledTask(id="heartbeat-real", prompt="Check this actual task", name="Heartbeat", conversation_id=owner.id,
                         workspace_root=str(tmp_path), isolation="workspace", kind="heartbeat", notification_policy=None)
    result = asyncio.run(scheduled_task_runner.run_scheduled_task(task, ScheduledTaskRun(id="run-real", conversation_id=owner.id), bootstrap=SimpleNamespace()))
    assert "<automation_id>heartbeat-real</automation_id>" in observed[0]["message"]
    assert observed[0]["automation_kind"] == "heartbeat"
    restored = ConversationRepository(base_dir=tmp_path / "data").get_conversation(owner.id)
    assert restored.has_unread_turn is False
    assert restored.transcript[-1]["content"] == ""
    assert restored.transcript[-1]["metadata"]["completion_notification"] == result["completion_notification"]
    assert result["completion_notification"]["last_agent_message"] == "DONT_NOTIFY"
    assert result["message_id"] == restored.transcript[-1]["id"]


def test_scheduled_update_omission_preserves_policy_and_explicit_null_unmutes(monkeypatch, tmp_path):
    from backend.tasks import scheduler as module
    monkeypatch.setattr(module, "SCHEDULE_FILE", tmp_path / "schedules.json")
    scheduler = TaskScheduler()
    task = scheduler.add_task("Actual task", "Original prompt", "0 * * * *", notification_policy="failed_runs_only", conversation_id="actual-target")
    configuration = dict(name=task.name, prompt=task.prompt, schedule=task.schedule, timezone="", isolation="workspace", conversation_id=task.conversation_id, permission_mode=task.permission_mode)
    scheduler.update_task(task.id, **configuration)
    assert TaskScheduler().list_tasks()[0]["notification_policy"] == "failed_runs_only"
    scheduler.update_task(task.id, **configuration, notification_policy=None)
    restored = TaskScheduler().list_tasks()[0]
    assert restored["notification_policy"] is None
    assert restored["kind"] == "heartbeat"
