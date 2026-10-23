from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType
from backend.services.scheduled_task_runner import run_scheduled_task
from backend.tasks import scheduler as scheduler_module
from backend.tasks.scheduler import ScheduledTask, ScheduledTaskRun, TaskScheduler, next_run_after
from backend.tests.test_scheduled_runtime_boundaries import scheduled_case


@pytest.mark.parametrize("rule,after,timezone,expected", [
    ("RRULE:FREQ=MINUTELY;INTERVAL=30", "2026-10-23T04:17:49+00:00", "Asia/Shanghai", "2026-10-23T04:47:00+00:00"),
    ("RRULE:FREQ=HOURLY;INTERVAL=2;BYMINUTE=0;BYDAY=MO,TU,WE,TH,FR,SA,SU", "2026-10-23T04:17:49+00:00", "Asia/Shanghai", "2026-10-23T06:00:00+00:00"),
    ("RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=0", "2026-10-23T04:17:49+00:00", "Asia/Shanghai", "2026-10-26T01:00:00+00:00"),
    ("DTSTART;TZID=America/New_York:20261001T000000\nRRULE:FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1;BYHOUR=9;BYMINUTE=15;COUNT=3", "2026-10-31T12:00:00+00:00", "Asia/Shanghai", "2026-11-30T14:15:00+00:00"),
    ('DTSTART;TZID="America/New_York":20261001T000000\nRRULE:FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1;BYHOUR=9;BYMINUTE=15;COUNT=3', "2026-10-31T12:00:00+00:00", "Asia/Shanghai", "2026-11-30T14:15:00+00:00"),
    ("DTSTART:20270101T000000Z\nRRULE:FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=1;BYHOUR=0;BYMINUTE=0;BYSECOND=0", "2026-10-23T04:17:49+00:00", "America/New_York", "2027-01-01T00:00:00+00:00"),
    ("DTSTART:20261025T090000\nRRULE:FREQ=WEEKLY;COUNT=2", "2026-10-23T04:17:49+00:00", "Asia/Shanghai", "2026-10-25T01:00:00+00:00"),
    ("RRULE:FREQ=DAILY;BYHOUR=9;BYMINUTE=0", "2026-11-01T12:30:00+00:00", "America/New_York", "2026-11-01T14:00:00+00:00"),
    ("RRULE:FREQ=YEARLY;INTERVAL=2;BYMONTH=6;BYMONTHDAY=1;BYHOUR=8;BYMINUTE=0", "2026-10-23T04:17:49+00:00", "UTC", "2028-06-01T08:00:00+00:00"),
])
def test_full_rrule_uses_timezone_explicit_start_and_stable_creation_minute(rule, after, timezone, expected):
    created = datetime(2026, 10, 23, 4, 17, 49, tzinfo=UTC)
    assert next_run_after(rule, datetime.fromisoformat(after), timezone=timezone, dtstart=created) == datetime.fromisoformat(expected)


def test_rrule_until_and_count_end_without_restarting_the_anchor():
    created = datetime(2026, 10, 23, 4, 17, 49, tzinfo=UTC)
    assert next_run_after("RRULE:FREQ=MINUTELY;INTERVAL=30;COUNT=3",
        datetime(2026, 10, 23, 5, 17, 1, tzinfo=UTC), timezone="Asia/Shanghai", dtstart=created) is None
    rule = "DTSTART;TZID=America/New_York:20261001T000000\nRRULE:FREQ=DAILY;BYHOUR=9;BYMINUTE=0;UNTIL=20261102T140000Z"
    assert next_run_after(rule, datetime(2026, 11, 2, 13, 59, tzinfo=UTC), timezone="Asia/Shanghai", dtstart=created) == datetime(2026, 11, 2, 14, 0, tzinfo=UTC)
    assert next_run_after(rule, datetime(2026, 11, 2, 14, 0, tzinfo=UTC), timezone="Asia/Shanghai", dtstart=created) is None
    with pytest.raises(ValueError):
        next_run_after("RRULE:FREQ=NOT_A_FREQUENCY", created, dtstart=created)


class RecordingModel(LLMAdapter):
    def __init__(self, inputs):
        self.inputs = inputs

    async def stream_chat(self, messages, tools=None, metadata=None):
        self.inputs.append(messages)
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Scheduled result", phase="final_answer")
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop", end_turn=True)

    async def simple_chat(self, messages):
        return "Scheduled result"


@pytest.mark.asyncio
async def test_rrule_cold_reload_dispatches_new_chats_in_the_selected_project_with_pinned_model(scheduled_case, monkeypatch):
    case = scheduled_case
    execution = case.workspace.parent / "execution-B"
    execution.mkdir()
    clock = {"now": datetime(2026, 10, 23, 4, 17, 49, tzinfo=UTC)}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"].astimezone(tz) if tz is not None else clock["now"].replace(tzinfo=None)

    monkeypatch.setattr(scheduler_module, "datetime", Clock)
    selections, inputs = [], []

    def create_model(*, config):
        selections.append((config.llm.provider, config.llm.model, config.llm.reasoning_effort))
        return RecordingModel(inputs)

    case.bootstrap.create_llm = create_model
    seed = TaskScheduler()
    task = seed.add_task("Pinned recurrence", "Run in B", "RRULE:FREQ=MINUTELY;INTERVAL=30;COUNT=3",
        workspace_root=str(case.workspace), execution_workspace_root=str(execution), isolation="workspace", timezone="Asia/Shanghai",
        provider="openai", model="gpt-5", reasoning_effort="high", notification_policy="failed_runs_only")
    assert task.next_run_at == "2026-10-23T04:47:00+00:00"

    async def fire(definition, run):
        return await run_scheduled_task(definition, run, bootstrap=case.bootstrap, bind_conversation=restored.bind_run_conversation)

    restored = TaskScheduler(on_fire=fire)
    for due in (datetime(2026, 10, 23, 4, 47, tzinfo=UTC), datetime(2026, 10, 23, 5, 17, tzinfo=UTC)):
        clock["now"] = due
        restored._tick(due)
        await next(iter(restored._run_tasks.values()))
        await asyncio.sleep(0)
    runs = restored.list_runs(workspace_root=str(case.workspace))
    assert len(runs) == 2 and len({run["conversation_id"] for run in runs}) == 2
    assert selections == [("openai", "gpt-5", "high")] * 2
    for run in runs:
        assert run["status"] == "completed" and run["workspace_root"] == str(case.workspace)
        assert run["execution_workspace_root"] == str(execution)
        conversation = case.repository.get_conversation(run["conversation_id"])
        assert conversation.execution_cwd == str(execution)
        assert conversation.model_selection["model"] == "gpt-5"
        assert conversation.model_selection["reasoning_effort"] == "high"
        assert run["completion_notification"]["automation_notification_decision"] == "DONT_NOTIFY"
    cold = TaskScheduler()
    assert cold._tasks[task.id].created_at == task.created_at
    assert cold._tasks[task.id].next_run_at is None
    clock["now"] = datetime(2026, 10, 23, 6, 0, tzinfo=UTC)
    cold._tick(clock["now"])
    assert len(cold.list_runs(workspace_root=str(case.workspace))) == 2


@pytest.mark.asyncio
async def test_heartbeat_targets_the_selected_chat_context_model_and_actual_execution_root(scheduled_case):
    case = scheduled_case
    execution = case.workspace.parent / "heartbeat-B"
    execution.mkdir()
    conversation = case.repository.create_conversation(workspace_root=str(execution), permission_mode="auto",
        model_selection={"provider": "openai", "model": "gpt-5-mini", "reasoning_effort": "low"},
        context_snapshot={"history": [{"role": "user", "content": "Retained heartbeat context"}]})
    selections, inputs = [], []

    def create_model(*, config):
        selections.append((config.llm.model, config.llm.reasoning_effort))
        return RecordingModel(inputs)

    case.bootstrap.create_llm = create_model
    task = ScheduledTask(workspace_root=str(case.workspace), conversation_id=conversation.id,
        kind="heartbeat", isolation="workspace", permission_mode="auto", prompt="Continue B", model="ignored-task-model", reasoning_effort="high")
    run = ScheduledTaskRun(task_id=task.id, workspace_root=str(case.workspace), turn_trigger="automation_heartbeat_scheduled")
    result = await run_scheduled_task(task, run, bootstrap=case.bootstrap)
    assert result["status"] == "completed" and result["conversation_id"] == conversation.id
    assert result["workspace_root"] == str(case.workspace) and result["execution_workspace_root"] == str(execution)
    assert selections == [("gpt-5-mini", "low")]
    assert "Retained heartbeat context" in str(inputs)


@pytest.mark.asyncio
async def test_projectless_schedule_uses_the_existing_generated_directory_on_each_run(scheduled_case):
    case = scheduled_case
    base = case.workspace.parent / "configured-task-directory"
    task = ScheduledTask(workspace_root=str(case.workspace), execution_workspace_root="", projectless_base=str(base),
        isolation="worktree", permission_mode="auto", prompt="Create deliverables")
    results = []
    for index in range(2):
        run = ScheduledTaskRun(id=f"projectless-{index}", task_id=task.id, workspace_root=str(case.workspace))
        results.append(await run_scheduled_task(task, run, bootstrap=case.bootstrap))
    assert len({result["execution_workspace_root"] for result in results}) == 2
    for result in results:
        assert result["status"] == "completed" and result["workspace_root"] == str(case.workspace)
        conversation = case.repository.get_conversation(result["conversation_id"])
        cwd = Path(result["execution_workspace_root"])
        assert cwd.is_relative_to(base) and (cwd / "outputs").is_dir() and (cwd / "work").is_dir()
        assert conversation.workspace_root == "" and conversation.execution_cwd == str(cwd)
        assert conversation.projectless_output_dir == str(cwd / "outputs")
    task.projectless_base = None
    with pytest.raises(ValueError, match="configured task directory"):
        await run_scheduled_task(task, ScheduledTaskRun(task_id=task.id), bootstrap=case.bootstrap)
