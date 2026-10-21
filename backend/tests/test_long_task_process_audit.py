from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys

import psutil
import pytest

from scripts.audit_long_task_session import (
    CHILD_PROBE, VERIFIER, ProbeProcessAudit, original_probe_handle_evidence, successful_file_reads,
)


SOLUTION = """from decimal import Decimal
def reconcile(rows):
    unique = {}
    for row in rows:
        unique.setdefault(row['id'], row)
    totals = {}
    for row in unique.values():
        totals[row['account']] = totals.get(row['account'], Decimal(0)) + Decimal(row['amount'])
    return {key: format(value, '.2f') for key, value in totals.items()}
"""


def test_auditor_import_does_not_bind_config_before_the_cli_selects_its_state_root():
    result = subprocess.run([sys.executable, "-X", "utf8", "-c",
        "import sys; import scripts.audit_long_task_session; assert 'backend.config' not in sys.modules"],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr


def case(tmp_path, *, seconds=2, solution=SOLUTION):
    state, workspace = tmp_path / "state", tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "verify.py").write_text(VERIFIER, encoding="utf-8")
    (workspace / "child_probe.py").write_text(CHILD_PROBE, encoding="utf-8")
    (workspace / "solution.py").write_text(solution, encoding="utf-8")
    rows = []
    observer = ProbeProcessAudit(os.getpid(), state, workspace, seconds, record=lambda direction, value: rows.append(value))
    return observer, state, workspace, rows


def launch(state, workspace, *, seconds=2, identity="original-command", unbuffered=True, **changed):
    process = subprocess.Popen([sys.executable, *(["-u"] if unbuffered else []), "verify.py", "--seconds", str(seconds)],
        cwd=workspace, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    parent = psutil.Process(os.getpid())
    payload = {"task_id": identity, "command": f"python -u verify.py --seconds {seconds}",
        "cwd": workspace.resolve().as_uri(), "pid": process.pid, "process_start_time": psutil.Process(process.pid).create_time(),
        "status": "running", "owner_pid": parent.pid, "owner_start_time": parent.create_time(),
        "conversation_id": "owner-a", "owner_task_id": "", "parent_run_id": "root-turn", **changed}
    folder = state / "data/agent-runtime/background_tasks/original-session"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{identity}.json").write_text(json.dumps(payload), encoding="utf-8")
    return process


async def retire(observer, *processes):
    observer.stopping = True
    for process in processes:
        if process.poll() is None:
            process.kill()
    if observer.task is not None:
        await asyncio.gather(observer.task, return_exceptions=True)
    await asyncio.gather(*(probe["wait"] for probe in observer.probes.values()), return_exceptions=True)
    for process in processes:
        process.wait(timeout=5)


@pytest.mark.asyncio
async def test_parent_observer_retains_the_original_os_identity_exit_and_offline_samples(tmp_path):
    observer, state, workspace, rows = case(tmp_path)
    observer.offline = True
    observer.start()
    process = launch(state, workspace)
    try:
        async with asyncio.timeout(8):
            while "verify.py" not in observer.probes or "exit_code" not in observer.probes["verify.py"]:
                observer.check()
                await asyncio.sleep(0.05)
        evidence = observer.evidence(conversation_id="owner-a", main_turn_id="root-turn")["verify.py"]
        verified = json.loads((workspace / "verification.json").read_text(encoding="utf-8"))
        assert evidence["pid"] == process.pid == verified["pid"]
        assert evidence["exit_code"] == 0 and evidence["observed_seconds"] >= 1.6
        assert evidence["command_id"] == "original-command" and evidence["offline_samples"] >= 2
        assert any(row["type"] == "probe.observed" and row["offline"] for row in rows)
        with pytest.raises(AssertionError, match="another conversation"):
            observer.evidence(conversation_id="owner-b", main_turn_id="root-turn")
        with pytest.raises(AssertionError, match="lost its turn"):
            observer.evidence(conversation_id="owner-a", main_turn_id="later-turn")
    finally:
        await retire(observer, process)


@pytest.mark.asyncio
async def test_model_written_success_files_cannot_replace_a_real_owned_process(tmp_path):
    observer, _state, workspace, _rows = case(tmp_path)
    (workspace / "verification.json").write_text(json.dumps({"passed": True, "elapsed_seconds": 3600, "pid": 123}), encoding="utf-8")
    observer.start()
    try:
        with pytest.raises(AssertionError, match="never ran"):
            observer.evidence(conversation_id="owner-a", main_turn_id="root-turn")
    finally:
        await retire(observer)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["backend-owner", "shell-pid-reuse", "shortened-seconds", "changed-python-arguments"])
async def test_probe_capture_rejects_another_owner_reused_pid_or_shorter_command(tmp_path, fault):
    observer, state, workspace, _rows = case(tmp_path)
    changed = {"owner_pid": os.getpid() + 1} if fault == "backend-owner" else {}
    process = launch(state, workspace, seconds=1 if fault == "shortened-seconds" else 2,
        unbuffered=fault != "changed-python-arguments", **changed)
    if fault == "shell-pid-reuse":
        path = next(observer.tasks_root.glob("*/*.json"))
        owner = json.loads(path.read_text(encoding="utf-8"))
        owner["process_start_time"] += 1
        path.write_text(json.dumps(owner), encoding="utf-8")
    try:
        with pytest.raises(AssertionError, match="another backend owner|PID was reused|shortened|command arguments"):
            observer.sample()
        assert not observer.probes
    finally:
        await retire(observer, process)


@pytest.mark.asyncio
async def test_original_probe_cannot_be_replaced_by_a_second_process_or_command_handle(tmp_path):
    observer, state, workspace, _rows = case(tmp_path)
    first = launch(state, workspace)
    second = None
    try:
        observer.sample()
        assert observer.probes["verify.py"]["identity"]["pid"] == first.pid
        second = launch(state, workspace, identity="replacement-command")
        with pytest.raises(AssertionError, match="restarted or replaced"):
            observer.sample()
    finally:
        await retire(observer, *([first, second] if second is not None else [first]))


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["foreign-uri", "native-path", "other-workspace-uri", "other-os-cwd"])
async def test_executor_uri_conversion_still_requires_the_original_local_workspace_and_actual_os_cwd(tmp_path, fault):
    observer, state, workspace, _rows = case(tmp_path)
    other = tmp_path / "other-workspace"
    other.mkdir()
    for source in workspace.iterdir():
        (other / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    changed = {"cwd": ("file:///tmp/foreign-executor" if os.name == "nt" else "file:///C:/foreign-executor")}
    if fault == "native-path":
        changed["cwd"] = str(workspace)
    elif fault == "other-workspace-uri":
        changed["cwd"] = other.as_uri()
    elif fault == "other-os-cwd":
        changed["cwd"] = workspace.as_uri()
    process = launch(state, other if fault == "other-os-cwd" else workspace, **changed)
    try:
        with pytest.raises((AssertionError, ValueError), match="foreign executor|absolute file URIs|another workspace|another source file"):
            observer.sample()
        assert not observer.probes
    finally:
        await retire(observer, process)


@pytest.mark.asyncio
async def test_actual_failed_oracle_exit_is_not_overruled_by_a_forged_success_json(tmp_path):
    observer, state, workspace, _rows = case(tmp_path, solution="import time\ntime.sleep(1)\ndef reconcile(rows): return {}\n")
    observer.start()
    process = launch(state, workspace)
    (workspace / "verification.json").write_text(json.dumps({"passed": True, "elapsed_seconds": 3600}), encoding="utf-8")
    try:
        with pytest.raises(AssertionError, match="process exited with 1"):
            async with asyncio.timeout(8):
                while True:
                    observer.check()
                    await asyncio.sleep(0.05)
    finally:
        await retire(observer, process)


def handle_case():
    processes = {"verify.py": {"command": f"& '{Path(sys.executable).as_posix()}' -u verify.py --seconds 60",
            "command_id": "main-handle", "parent_run_id": "root-turn", "owner_task_id": "root-task"},
        "child_probe.py": {"command": "python -u child_probe.py --seconds 60", "command_id": "child-handle",
            "owner_task_id": "original-child", "parent_run_id": "child-query-initial", "create_time": 100, "exited_at": 170}}
    events = []
    for identity, name, args, code in (("launch", "run_command", {"command": processes["verify.py"]["command"], "run_in_background": True}, None),
            ("poll", "monitor", {"command_id": "main-handle"}, 0), ("spawn", "spawn_agent", {"task_name": "reviewer"}, None),
            ("followup", "followup_task", {"target": "/root/reviewer"}, None)):
        call = {"type": "tool_call", "id": identity, "name": name, "args": args, "turn_id": "root-turn",
            "task_id": "root-task", "conversation_id": "owner-a", "message_id": "answer", "status": "running",
            "streaming_announcement": False}
        preview = {**call, "args": {key: value for key, value in args.items() if key != "run_in_background"}, "status": "pending"}
        preview.pop("streaming_announcement")
        events.extend([{**call, "args": {}, "status": "pending", "streaming_announcement": True}, preview, call,
            {"type": "tool_result", "id": identity, "exit_code": code, "is_error": False, "status": "success",
                "summary": "Opaque public result without command bookkeeping", "task_id": "root-task",
                "conversation_id": "owner-a", "message_id": "answer", "turn_id": "root-turn"}])
    for kind in ("background.started", "background.completed"):
        events.append({"type": kind, "command_id": "main-handle", "run_id": "main-handle", "task_id": "root-task",
            "parent_run_id": "root-turn", "conversation_id": "owner-a", "command": processes["verify.py"]["command"][:100],
            "incarnation": "original-main-command", "status": "completed" if kind.endswith("completed") else "running", "exit_code": 0})
    for epoch, started, completed, parent in ((1, 99000, 110000, "spawn"), (2, 171000, 180000, "followup")):
        scope = {"subagent_id": "original-child", "run_id": "original-child", "task_id": "original-child",
            "parent_run_id": "root-turn", "conversation_id": "owner-a", "parent_message_id": "answer",
            "parent_tool_call_id": parent, "agent_path": "root-turn/reviewer", "mailbox_epoch": epoch, "started_at": started}
        events.extend([{**scope, "type": "subagent.start", "status": "running"},
            {**scope, "type": "subagent.done", "status": "completed", "completed_at": completed}])
    messages = [{"id": "subagent-turn-child-query-initial", "role": "assistant", "turn_id": "child-query-initial", "timestamp": 101000,
        "completed_at": 109000, "terminal_status": "completed",
        "blocks": [{"type": "tool_call", "record": {"id": "child-launch", "name": "run_command", "status": "success",
            "args": {"command": processes["child_probe.py"]["command"]}}}]},
        {"id": "subagent-turn-child-query-followup", "role": "assistant", "turn_id": "child-query-followup", "timestamp": 172000,
        "completed_at": 179000, "terminal_status": "completed",
        "blocks": [{"type": "tool_call", "record": {"id": "child-poll", "name": "monitor", "status": "success",
            "args": {"command_id": "child-handle"}, "exitCode": 0, "outputPreview": "CHILD_PROBE_OK"}}]}]
    transcript = {"subagent_id": "original-child", "conversation_id": "owner-a", "agent_path": "root-turn/reviewer",
        "mailbox_epoch": 2, "seq": 50, "messages": messages}
    return processes, events, transcript


@pytest.mark.parametrize("fault", [None, "main-handle", "main-exit", "main-owner", "main-result-turn", "main-result-task",
    "main-launch-error", "main-preview-only", "main-relaunch", "main-missing-start", "main-missing-completion", "main-lifecycle-owner",
    "main-lifecycle-turn", "main-lifecycle-task", "main-lifecycle-command", "main-lifecycle-incarnation", "main-lifecycle-exit",
    "child-owner", "child-tree", "child-turn", "child-handle", "child-exit", "child-launch-error", "child-relaunch",
    "child-actor", "child-final-actor", "child-final-parent", "child-final-epoch", "child-transcript-owner", "child-transcript-path",
    "child-transcript-epoch", "child-followup-turn", "child-followup-before-final", "no-followup", "no-followup-start"])
def test_handle_acceptance_requires_actual_original_launch_owner_and_completed_followup_poll(fault):
    processes, events, transcript = handle_case()
    messages = transcript["messages"]
    def call(identity):
        return next(event for event in events if event.get("type") == "tool_call" and event["id"] == identity
            and event.get("streaming_announcement") is False)
    def result(identity):
        return next(event for event in events if event.get("type") == "tool_result" and event["id"] == identity)
    def lifecycle(kind, epoch=None):
        return next(event for event in events if event["type"] == kind and (epoch is None or event["mailbox_epoch"] == epoch))
    if fault == "main-handle":
        call("poll")["args"]["command_id"] = "replacement-handle"
    elif fault == "main-exit":
        result("poll")["exit_code"] = 1
    elif fault == "main-owner":
        result("poll")["conversation_id"] = "other-owner"
    elif fault == "main-result-turn":
        result("poll")["turn_id"] = "another-turn"
    elif fault == "main-result-task":
        result("poll")["task_id"] = "another-task"
    elif fault == "main-launch-error":
        result("launch")["is_error"] = True
    elif fault == "main-preview-only":
        events.remove(call("launch"))
    elif fault == "main-relaunch":
        events.extend([{**deepcopy(call("launch")), "id": "second-launch"}, {**result("launch"), "id": "second-launch"}])
    elif fault in {"main-missing-start", "main-missing-completion"}:
        events.remove(lifecycle("background.started" if fault.endswith("start") else "background.completed"))
    elif fault and fault.startswith("main-lifecycle-"):
        key = {"owner": "conversation_id", "turn": "parent_run_id", "task": "task_id", "command": "command",
            "incarnation": "incarnation", "exit": "exit_code"}[fault.removeprefix("main-lifecycle-")]
        lifecycle("background.completed")[key] = 1 if key == "exit_code" else "different"
    elif fault == "child-owner":
        lifecycle("subagent.start", 1)["parent_message_id"] = "another-answer"
    elif fault == "child-tree":
        lifecycle("subagent.start", 1)["agent_path"] = "another-root/reviewer"
    elif fault == "child-turn":
        messages[0]["turn_id"] = "another-reviewer-turn"
    elif fault == "child-handle":
        messages[1]["blocks"][0]["record"]["args"]["command_id"] = "replacement-handle"
    elif fault == "child-exit":
        messages[1]["blocks"][0]["record"]["exitCode"] = 1
    elif fault == "child-launch-error":
        messages[0]["blocks"][0]["record"]["status"] = "failed"
    elif fault == "child-relaunch":
        messages[1]["blocks"].append(deepcopy(messages[0]["blocks"][0]))
    elif fault == "child-actor":
        lifecycle("subagent.start", 1)["run_id"] = "child-query-initial"
    elif fault == "child-final-actor":
        lifecycle("subagent.done", 2)["run_id"] = "another-child"
    elif fault == "child-final-parent":
        lifecycle("subagent.done", 2)["parent_tool_call_id"] = "spawn"
    elif fault == "child-final-epoch":
        lifecycle("subagent.done", 2)["mailbox_epoch"] = 3
    elif fault == "child-transcript-owner":
        transcript["subagent_id"] = "another-child"
    elif fault == "child-transcript-path":
        transcript["agent_path"] = "another-root/reviewer"
    elif fault == "child-transcript-epoch":
        transcript["mailbox_epoch"] = 3
    elif fault == "child-followup-turn":
        messages[1]["turn_id"] = "child-query-initial"
    elif fault == "child-followup-before-final":
        messages[1]["timestamp"] = 105000
    elif fault == "no-followup":
        events.remove(lifecycle("subagent.done", 2))
    elif fault == "no-followup-start":
        events.remove(lifecycle("subagent.start", 2))
    if fault:
        with pytest.raises(AssertionError):
            original_probe_handle_evidence(processes, events, conversation_id="owner-a", message_id="answer", child_transcript=transcript)
    else:
        assert not any("command_id" in event for event in events if event["type"] == "tool_result")
        receipt = original_probe_handle_evidence(processes, events, conversation_id="owner-a", message_id="answer", child_transcript=transcript)
        assert receipt["child_id"] == "original-child" and receipt["child_followup_poll_call_ids"] == ["child-poll"]
        assert receipt["child_launch_turn_id"] == "child-query-initial" and receipt["child_mailbox_epochs"] == [1, 2]


def test_read_receipts_use_the_actual_final_call_after_empty_and_argument_previews():
    scope = {"id": "read", "name": "read_file", "conversation_id": "owner", "message_id": "answer"}
    events = [
        {**scope, "type": "tool_call", "args": {}, "status": "pending", "streaming_announcement": True},
        {**scope, "type": "tool_call", "args": {"file_path": "preview.txt"}, "status": "pending"},
        {**scope, "type": "tool_call", "args": {"file_path": "progress.jsonl"}, "status": "running", "streaming_announcement": False},
        {**scope, "type": "tool_result", "status": "success", "is_error": False},
    ]
    assert successful_file_reads(events, conversation_id="owner", message_id="answer", required_files=("progress.jsonl",)) == {"progress.jsonl": "read"}
