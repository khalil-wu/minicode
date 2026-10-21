from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import sys

import psutil
import pytest

from scripts.audit_long_task_session import (
    CHILD_PROBE, VERIFIER, ProbeProcessAudit, original_probe_handle_evidence,
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
        "cwd": str(workspace), "pid": process.pid, "process_start_time": psutil.Process(process.pid).create_time(),
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
    processes = {"verify.py": {"command": "python -u verify.py --seconds 60", "command_id": "main-handle", "parent_run_id": "root-turn"},
        "child_probe.py": {"command": "python -u child_probe.py --seconds 60", "command_id": "child-handle",
            "owner_task_id": "original-child", "parent_run_id": "child-initial", "create_time": 100, "exited_at": 170}}
    events = []
    for identity, name, args, code in (("launch", "Bash", {"command": processes["verify.py"]["command"]}, None),
        ("poll", "monitor", {"command_id": "main-handle"}, 0)):
        events.extend([{"type": "tool_call", "id": identity, "name": name, "args": args, "turn_id": "root-turn",
            "conversation_id": "owner-a", "message_id": "answer"},
            {"type": "tool_result", "id": identity, "command_id": "main-handle", "exit_code": code, "is_error": False,
                "conversation_id": "owner-a", "message_id": "answer", "turn_id": "root-turn"}])
    events.append({"type": "subagent.start", "subagent_id": "original-child", "run_id": "child-initial", "parent_run_id": "root-turn",
        "conversation_id": "owner-a", "parent_message_id": "answer"})
    for epoch, timestamp in ((1, 110), (2, 180)):
        events.append({"type": "subagent.done", "subagent_id": "original-child", "status": "completed", "mailbox_epoch": epoch,
            "timestamp": datetime.fromtimestamp(timestamp, UTC).isoformat()})
    messages = [{"role": "assistant", "turn_id": "child-initial", "timestamp": 101000,
        "blocks": [{"type": "tool_call", "record": {"id": "child-launch", "name": "Bash", "status": "success",
            "args": {"command": processes["child_probe.py"]["command"]}}}]},
        {"role": "assistant", "turn_id": "child-followup", "timestamp": 171000,
        "blocks": [{"type": "tool_call", "record": {"id": "child-poll", "name": "monitor", "status": "success",
            "args": {"command_id": "child-handle"}, "exitCode": 0, "outputPreview": "CHILD_PROBE_OK"}}]}]
    return processes, events, messages


@pytest.mark.parametrize("fault", [None, "main-handle", "main-exit", "main-owner", "main-result-turn", "child-owner", "child-turn", "child-handle", "child-exit", "child-relaunch", "no-followup"])
def test_handle_acceptance_requires_actual_original_launch_owner_and_completed_followup_poll(fault):
    processes, events, messages = handle_case()
    if fault == "main-handle":
        events[2]["args"]["command_id"] = "replacement-handle"
    elif fault == "main-exit":
        events[3]["exit_code"] = 1
    elif fault == "main-owner":
        events[3]["conversation_id"] = "other-owner"
    elif fault == "main-result-turn":
        events[3]["turn_id"] = "another-turn"
    elif fault == "child-owner":
        events[4]["parent_message_id"] = "another-answer"
    elif fault == "child-turn":
        messages[0]["turn_id"] = "another-reviewer-turn"
    elif fault == "child-handle":
        messages[1]["blocks"][0]["record"]["args"]["command_id"] = "replacement-handle"
    elif fault == "child-exit":
        messages[1]["blocks"][0]["record"]["exitCode"] = 1
    elif fault == "child-relaunch":
        messages[1]["blocks"].append(deepcopy(messages[0]["blocks"][0]))
    elif fault == "no-followup":
        events.pop()
    if fault:
        with pytest.raises(AssertionError):
            original_probe_handle_evidence(processes, events, conversation_id="owner-a", message_id="answer", child_messages=messages)
    else:
        receipt = original_probe_handle_evidence(processes, events, conversation_id="owner-a", message_id="answer", child_messages=messages)
        assert receipt["child_id"] == "original-child" and receipt["child_followup_poll_call_ids"] == ["child-poll"]
