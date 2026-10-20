"""Run a real model and real backend through long work, navigation and reconnect.

Only the configured provider is read from --configured-state. Credentials stay
in process memory/environment. All chats, files and traces belong to --out.
No desktop application or browser is started.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from datetime import datetime
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def backend_source_snapshot():
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((ROOT / "backend").rglob("*.py")) if "tests" not in path.parts}

VERIFIER = '''import argparse
import importlib.util
import json
from decimal import Decimal
from pathlib import Path
import random
import time

parser = argparse.ArgumentParser()
parser.add_argument("--seconds", type=int, required=True)
args = parser.parse_args()
spec = importlib.util.spec_from_file_location("solution", "solution.py")
solution = importlib.util.module_from_spec(spec)
spec.loader.exec_module(solution)
rng = random.Random(20261020)
started = time.monotonic()
batches = 0
assert solution.reconcile([]) == {}
with Path("progress.jsonl").open("w", encoding="utf-8") as progress:
    while time.monotonic() - started < args.seconds:
        rows = [{"id": str(rng.randrange(450)), "account": "acct-" + str(rng.randrange(8)),
                 "amount": str(Decimal(rng.randrange(-100000, 100001)) / 100)} for _ in range(600)]
        rows.extend([{"id": "zero-positive", "account": "zero-total", "amount": "10.00"},
                     {"id": "zero-negative", "account": "zero-total", "amount": "-10.00"}])
        first = {}
        for row in rows:
            first.setdefault(row["id"], row)
        expected = {}
        for row in first.values():
            expected[row["account"]] = expected.get(row["account"], Decimal(0)) + Decimal(row["amount"])
        expected = {key: format(value, ".2f") for key, value in sorted(expected.items())}
        original = json.dumps(rows, sort_keys=True)
        actual = solution.reconcile(rows)
        assert actual == expected, {"batch": batches, "actual": actual, "expected": expected}
        assert json.dumps(rows, sort_keys=True) == original, "Input rows were mutated"
        batches += 1
        entry = {"batch": batches, "elapsed": round(time.monotonic() - started, 3), "checked_rows": batches * 602}
        progress.write(json.dumps(entry) + "\\n")
        progress.flush()
        print(json.dumps(entry), flush=True)
        time.sleep(1)
result = {"passed": True, "batches": batches, "checked_rows": batches * 602,
          "elapsed_seconds": round(time.monotonic() - started, 3)}
Path("verification.json").write_text(json.dumps(result), encoding="utf-8")
print("INDEPENDENT_ORACLE_OK", json.dumps(result), flush=True)
'''

CHILD_PROBE = '''import json
import os
from pathlib import Path
import time
started = time.monotonic()
with Path("child-progress.jsonl").open("w", encoding="utf-8") as progress:
    while time.monotonic() - started < 90:
        row = {"pid": os.getpid(), "timestamp": time.time(), "elapsed": time.monotonic() - started}
        progress.write(json.dumps(row) + "\\n")
        progress.flush()
        print(json.dumps(row), flush=True)
        time.sleep(1)
Path("child-result.json").write_text(json.dumps({"passed": True, "pid": os.getpid(), "elapsed": time.monotonic()-started}))
print("CHILD_PROBE_OK", flush=True)
'''


class Client:
    def __init__(self, url: str, trace, secret: str):
        self.url, self.trace, self.secret = url, trace, secret
        self.events: list[dict] = []
        self.last_seq = 0
        self.ws = None
        self.reader = None
        self._recovering = False
        self._recovery_command_id = None
        self._recovery_snapshot = None
        self._recovery_replay_pending = False
        self._recovery_owner_pending = False
        self._recovery_events: list[dict] = []
        self._delivered_event_ids: dict[str, int] = {}
        self._snapshot_seq = 0

    def record(self, direction: str, payload: dict):
        row = {"direction": direction, "elapsed": round(time.monotonic() - self.started, 3), "payload": payload}
        self.trace.write(json.dumps(row, ensure_ascii=False).replace(self.secret, "[redacted]") + "\n")
        self.trace.flush()

    async def connect(self):
        import websockets
        if self.reader is not None:
            self._begin_recovery()
        self.ws = await websockets.connect(self.url, max_size=None, ping_timeout=60)
        self.reader = asyncio.create_task(self.read())

    def _begin_recovery(self):
        # Match useWebSocket's open/restore gate: live durable frames can
        # arrive before the canonical owner and its replay are projected.
        self._recovering = True
        self._recovery_command_id = None
        self._recovery_snapshot = None
        self._recovery_replay_pending = False
        self._recovery_owner_pending = False
        self._recovery_events = []

    def _deliver_durable(self, event):
        event_id = event.get("event_id")
        if event_id in self._delivered_event_ids:
            assert self._delivered_event_ids[event_id] == event["seq"], event
            return
        if event["seq"] <= self.last_seq:
            # An empty-replay snapshot is the authoritative projection through
            # current_seq. Buffered frames inside that snapshot are covered.
            assert event["seq"] <= self._snapshot_seq, event
            return
        assert event["previous_replay_seq"] == self.last_seq and event["seq"] > self.last_seq, event
        self.last_seq = event["seq"]
        if event_id:
            self._delivered_event_ids[event_id] = event["seq"]
        self.events.append(event)

    def _finish_recovery(self):
        if (self._recovery_snapshot is None or self._recovery_replay_pending
                or self._recovery_owner_pending):
            return
        buffered, self._recovery_events = self._recovery_events, []
        for event in buffered:
            self._deliver_durable(event)
        self._recovering = False
        self._recovery_command_id = None

    def _receive(self, event):
        kind = event["type"]
        recovery_projection = kind in {"session.restored", "session.synced", "session.replay"}
        if self._recovering and recovery_projection and event.get("client_command_id") != self._recovery_command_id:
            return  # Its raw frame remains in the trace; it is not our restore.
        if (self._recovering and kind == "command.result"
                and event.get("client_command_id") == self._recovery_command_id
                and event.get("level") in {"error", "failed"}):
            self.events.append(event)
            raise AssertionError(event)
        if self._recovering and "previous_replay_seq" in event and not recovery_projection:
            self._recovery_events.append(event)
            return
        if kind == "session.replay":
            if self._recovering:
                assert self._recovery_snapshot is not None and self._recovery_replay_pending, event
                assert event["current_seq"] == self._recovery_snapshot["current_seq"], event
                assert event["replayed_events"] == self._recovery_snapshot["replayed_events"], event
            assert event["last_seq"] == self.last_seq, event
            assert event["replayed_events"] == len(event["events"]), event
            for replayed in event["events"]:
                assert replayed["previous_replay_seq"] == self.last_seq and replayed["seq"] > self.last_seq, replayed
                self._deliver_durable(replayed)
            assert self.last_seq == event["current_seq"], event
            self._recovery_replay_pending = False
        elif kind in {"session.restored", "session.synced"}:
            if event["replayed_events"] == 0:
                if event.get("cursor_reset") is True:
                    assert event["requested_last_seq"] > event["current_seq"] == event["last_seq"], event
                    self._delivered_event_ids.clear()
                else:
                    assert event["last_seq"] == self.last_seq and event["current_seq"] >= self.last_seq, event
                self.last_seq = event["current_seq"]
                self._snapshot_seq = self.last_seq
            if self._recovering:
                self._recovery_snapshot = event
                self._recovery_replay_pending = event["replayed_events"] > 0
                self._recovery_owner_pending = kind == "session.restored" and event.get("conversation_switched_follows") is True
        elif "previous_replay_seq" in event:
            self._deliver_durable(event)
            return
        elif (self._recovering and kind == "conversation.switched"
                and event.get("client_command_id") == self._recovery_command_id
                and self._recovery_snapshot is not None
                and event.get("conversation_id") == self._recovery_snapshot.get("active_conversation_id")):
            self._recovery_owner_pending = False
        self.events.append(event)
        if self._recovering:
            self._finish_recovery()

    async def read(self):
        async for raw in self.ws:
            event = json.loads(raw)
            self.record("server", event)
            self._receive(event)

    async def send(self, kind: str, **data):
        payload = {"type": kind, "client_command_id": uuid4().hex, **data}
        if kind in {"session.restore", "session.sync"}:
            if not self._recovering:
                self._begin_recovery()
            self._recovery_command_id = payload["client_command_id"]
        self.record("client", payload)
        await self.ws.send(json.dumps(payload))
        return payload["client_command_id"]

    async def until(self, predicate, label: str, *, after=0, timeout=300):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.reader.done():
                await self.reader
            for event in self.events[after:]:
                if predicate(event):
                    return event
            if self.reader.done():
                raise RuntimeError(f"Transport ended while awaiting {label}")
            await asyncio.sleep(0.1)
        raise TimeoutError(label)

    async def command(self, kind: str, **data):
        offset = len(self.events)
        command_id = await self.send(kind, **data)
        projection = {"conversation.switch": "conversation.switched", "session.restore": "session.restored", "session.sync": "session.synced"}.get(kind)
        if projection:
            result = await self.until(lambda event: event.get("type") == projection
                and event.get("client_command_id") == command_id
                and (kind != "conversation.switch" or event.get("conversation_id") == data["conversation_id"]), kind, after=offset)
            if kind == "session.restore" and result.get("conversation_switched_follows") is True:
                # useWebSocket releases this gate at the canonical owner receipt;
                # is_hydrating tracks the subsequent context hydration separately.
                await self.until(lambda event: event.get("type") == "conversation.switched"
                    and event.get("client_command_id") == command_id
                    and event.get("conversation_id") == result.get("active_conversation_id"),
                    "canonical restored owner", after=offset)
            if kind in {"session.restore", "session.sync"} and result["replayed_events"]:
                await self.until(lambda event: event.get("type") == "session.replay"
                    and event.get("client_command_id") == command_id
                    and event["current_seq"] == result["current_seq"], "complete session replay", after=offset)
            return result
        result = await self.until(lambda event: event.get("type") == "command.result"
            and event.get("client_command_id") == command_id, kind, after=offset)
        assert result.get("level") not in {"error", "failed"}, result
        return result

    async def terminal(self, owner: str, message_id: str):
        def completed(event):
            if event.get("type") == "done" and event.get("conversation_id") == owner and event.get("message_id") == message_id:
                assert event["status"] == "completed", event
                return True
            if event.get("type") in {"session.restored", "session.synced"} and event.get("active_conversation_id") == owner:
                return any(message["id"] == message_id and message.get("terminal_status") == "completed"
                           for message in event.get("messages", []))
            if event.get("type") == "conversation.switched" and event.get("conversation_id") == owner:
                return any(message["id"] == message_id and message.get("terminal_status") == "completed"
                           for message in event["conversation"]["transcript"])
            return False
        return await self.until(completed, "terminal " + message_id, timeout=420)

    async def close(self):
        await self.ws.close()
        await self.reader


async def exercise(args, config):
    import httpx
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    state, workspace = out / "state", out / "workspace"
    data = state / "data"
    data.mkdir(parents=True)
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    (workspace / "verify.py").write_text(VERIFIER, encoding="utf-8")
    if args.child_probe:
        (workspace / "child_probe.py").write_text(CHILD_PROBE, encoding="utf-8")
    side_identity = "side-identity-" + uuid4().hex
    (workspace / "B_NOTE.txt").write_text(side_identity, encoding="utf-8")
    requirements = ("Implement solution.py with reconcile(rows). Each row is a dict with string id, account, amount. "
        "For duplicate ids keep the first row globally, including when later duplicates name another account. "
        "Sum signed amounts exactly with Decimal per account. Return account -> amount string with exactly two "
        "fractional digits. Include accounts whose total is zero. Input order and duplicate conflicts must work. "
        "Do not mutate the caller's rows. Use only the Python standard library.\n")
    (workspace / "REQUIREMENTS.md").write_text(requirements, encoding="utf-8")
    profile = {"llm": {"provider": "custom", "custom": {"base_url": config.llm.base_url,
        "model": config.llm.model, "wire_api": config.llm.wire_api,
        "available_models": [config.llm.model], "reasoning_effort": config.llm.reasoning_effort,
        "context_window": config.llm.context_window, "proxy_mode": config.llm.proxy_mode}},
        "agent": {"max_turn_seconds": 600}}
    (state / "settings.json").write_text(json.dumps(profile), encoding="utf-8")
    (data / "trusted_workspaces.json").write_text(json.dumps({"version": 1, "roots": [str(workspace)]}), encoding="utf-8")
    (data / "active_workspace.json").write_text(json.dumps({"root": str(workspace)}), encoding="utf-8")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = dict(os.environ, MINICODE_STATE_ROOT=str(state), PYTHONUTF8="1", LLM_PROVIDER="custom",
        CUSTOM_API_KEY=config.llm.api_key, CUSTOM_BASE_URL=config.llm.base_url,
        CUSTOM_MODEL=config.llm.model, CUSTOM_WIRE_API=config.llm.wire_api)
    # This isolated loopback process uses the normal tokenless-local auth path.
    env.pop("MINICODE_RUNTIME_TOKEN", None)
    run_id = uuid4().hex
    a, b = "", ""
    messages = {"a": "assistant_a_" + run_id, "b": "assistant_b_" + run_id, "queue": "assistant_q_" + run_id}
    result = {"model": config.llm.model, "wire_api": config.llm.wire_api,
        "message_ids": messages, "passed": False}
    started = time.monotonic()
    source_snapshot = backend_source_snapshot()
    (out / "source-snapshot.json").write_text(json.dumps(source_snapshot, indent=2), encoding="utf-8")
    with (out / "backend.log").open("w", encoding="utf-8") as backend_log, (out / "wire.jsonl").open("w", encoding="utf-8") as trace:
        process = subprocess.Popen([sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1",
            "--port", str(port), "--log-level", "warning"], cwd=ROOT, env=env, stdout=backend_log,
            stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        client = Client(f"ws://127.0.0.1:{port}/ws?session_id=session_audit_{run_id}", trace, config.llm.api_key)
        client.started = started
        backend_ready = False
        try:
            async with httpx.AsyncClient(trust_env=False) as http:
                deadline = time.monotonic() + 60
                while True:
                    try:
                        response = await http.get(f"http://127.0.0.1:{port}/health")
                        if response.is_success:
                            break
                    except httpx.ConnectError:
                        pass  # Expected before uvicorn begins listening.
                    assert process.poll() is None, "Backend exited during startup; see backend.log"
                    if time.monotonic() > deadline:
                        raise TimeoutError("Backend startup; see backend.log")
                    await asyncio.sleep(0.2)
            backend_ready = True
            await client.connect()
            await client.send("ping")
            await client.until(lambda e: e.get("type") == "pong", "initial handshake")
            for label, title, activate in (("a", "Long reconciliation task", True), ("b", "Independent side task", False)):
                created = await client.command("conversation.create", title=title,
                    workspace_root=str(workspace), permission_mode="bypass", activate=activate)
                identity = created["data"]["conversation_id"]
                if label == "a":
                    a = identity
                else:
                    b = identity
                result["conversation_" + label] = identity
                await client.command("conversation.memory_mode.set", conversation_id=identity, memory_mode="disabled")
            python_command = Path(sys.executable).as_posix()
            prompt = ("Work only in the provided workspace. Read REQUIREMENTS.md, implement solution.py, and verify it. "
                "Do not edit verify.py, B_NOTE.txt or REQUIREMENTS.md. Delegate one read-only review of your solution "
                "to an available subagent and integrate any defects it actually finds. Use the real tools. "
                f"Run exactly this PowerShell command: & '{python_command}' -u verify.py --seconds {args.seconds}\n"
                "This verification deliberately runs longer than a tool's first yield. Keep following its actual command handle "
                "until it exits, inspect verification.json, and wait for your reviewer. Do not send a final answer while work "
                "is running. Only after independent verification passes, finish with A_LONG_OK and the actual batch count.")
            if args.child_probe:
                prompt += ("\nAlso verify the reviewer's persistent thread resources. In its INITIAL review task, require it "
                    f"to launch exactly once `& '{python_command}' -u child_probe.py` using the real command tool with a short yield. "
                    "Have the reviewer return its review and actual running command handle in a final answer while that "
                    "90-second probe is still running. The probe only writes child-progress.jsonl/child-result.json in this "
                    "workspace; do not edit child_probe.py or change the business solution as part of the read-only review. "
                    "Do NOT close or interrupt the reviewer after its initial final. After the main verifier completes, "
                    "send a real follow-up task to THE SAME reviewer asking it to poll its ORIGINAL command handle and "
                    "read child-result.json, without rerunning or replacing the probe. Wait for the actual follow-up "
                    "completion and CHILD_PROBE_OK before your own final answer.")
            await client.command("user_message", conversation_id=a, content=prompt, permission_mode="bypass",
                user_message_id="user_a_" + run_id, assistant_message_id=messages["a"])
            deadline = time.monotonic() + 360
            while not (workspace / "progress.jsonl").exists():
                if any(e.get("type") == "done" and e.get("message_id") == messages["a"] for e in client.events):
                    raise AssertionError("Main turn ended before starting the independent long verifier")
                if time.monotonic() > deadline:
                    raise TimeoutError("Real model did not start the verifier")
                await asyncio.sleep(0.2)
            result["long_tool_started_at"] = round(time.monotonic() - started, 3)
            print("Long verifier started; switching to B, queuing A follow-up and disconnecting", flush=True)
            await client.command("conversation.switch", conversation_id=b)
            await client.command("user_message", conversation_id=b,
                content="Read B_NOTE.txt with the file tool. Reply B_SIDE_OK followed by its exact contents. Do not edit any file.",
                permission_mode="bypass", user_message_id="user_b_" + run_id, assistant_message_id=messages["b"])
            queued = await client.command("user_message", conversation_id=a, content="After the previous task completes, read verification.json. Reply A_QUEUE_OK with the verified batch count. Do not edit anything.",
                permission_mode="bypass", queue_if_busy=True, streaming_behavior="follow_up",
                user_message_id="user_q_" + run_id, assistant_message_id=messages["queue"])
            assert queued["data"]["queued"] is True, queued
            for identity in (a, b, a, b):
                await client.command("conversation.switch", conversation_id=identity)
            before = len((workspace / "progress.jsonl").read_text(encoding="utf-8").splitlines())
            cursor = client.last_seq
            await client.close()
            client.record("audit", {"type": "offline", "last_seq": cursor, "progress_batches": before})
            await asyncio.sleep(36)
            after = len((workspace / "progress.jsonl").read_text(encoding="utf-8").splitlines())
            assert after > before + 20, ("Verifier stopped during transport retirement", before, after)
            result["offline_progress"] = {"before": before, "after": after, "offline_seconds": 36}
            await client.connect()
            await client.command("session.restore", last_seq=cursor, last_conversation_id=b)
            client.record("audit", {"type": "reconnected", "active_conversation_id": b})
            for identity in (a, b, a, b):
                await client.command("conversation.switch", conversation_id=identity)
            for label, message_id in messages.items():
                terminal = await client.terminal(b if label == "b" else a, message_id)
                result.setdefault("terminal_evidence", {})[label] = {"type": terminal["type"], "event_id": terminal["event_id"]}
            queue_receipts = [event for event in client.events if event.get("type") == "command.result"
                and event.get("client_command_id") == queued["client_command_id"]]
            assert any(event.get("data", {}).get("admitted") is True for event in queue_receipts)
            assert all(event["data"]["client_command_id"] == queued["client_command_id"] for event in queue_receipts)
            result["queue_origin"] = {"client_command_id": queued["client_command_id"],
                "receipt_event_ids": sorted({event["event_id"] for event in queue_receipts}),
                "nested_identity_matches": True}
            await client.command("conversation.switch", conversation_id=a)
            await client.command("session.sync", last_seq=client.last_seq)
            await client.close()
            assert (workspace / "verify.py").read_text(encoding="utf-8") == VERIFIER, "Oracle was modified"
            verified = json.loads((workspace / "verification.json").read_text(encoding="utf-8"))
            assert verified["passed"] and verified["elapsed_seconds"] >= args.seconds
            from backend.conversations.repository import ConversationRepository
            repository = ConversationRepository(data / "conversations")
            records = {key: repository.get_conversation(identity) for key, identity in (("a", a), ("b", b))}
            by_id = {message["id"]: message for record in records.values() for message in record.transcript}
            for label, marker in (("a", "A_LONG_OK"), ("b", "B_SIDE_OK"), ("queue", "A_QUEUE_OK")):
                assert marker in by_id[messages[label]]["content"], (label, by_id[messages[label]])
            assert side_identity in by_id[messages["b"]]["content"]
            assert str(verified["batches"]) in by_id[messages["queue"]]["content"]
            assert all(message["id"] != messages["b"] for message in records["a"].transcript)
            assert all(message["id"] not in {messages["a"], messages["queue"]} for message in records["b"].transcript)
            reviewers = {event["subagent_id"] for event in client.events if event.get("type") == "subagent.start"}
            completed_reviewers = {event["subagent_id"] for event in client.events
                if event.get("type") == "subagent.done" and event.get("status") == "completed"}
            assert reviewers and reviewers <= completed_reviewers, (reviewers, completed_reviewers)
            if args.child_probe:
                assert (workspace / "child_probe.py").read_text(encoding="utf-8") == CHILD_PROBE
                probe = [json.loads(line) for line in (workspace / "child-progress.jsonl").read_text(encoding="utf-8").splitlines()]
                child_result = json.loads((workspace / "child-result.json").read_text(encoding="utf-8"))
                first_done = next(event for event in client.events if event.get("type") == "subagent.done"
                    and event.get("status") == "completed")
                finished_at = datetime.fromisoformat(first_done["timestamp"]).timestamp()
                epochs = {event["mailbox_epoch"] for event in client.events if event.get("type") == "subagent.done"
                    and event.get("subagent_id") == first_done["subagent_id"] and event.get("status") == "completed"}
                assert len(epochs) >= 2 and len({row["pid"] for row in probe}) == 1
                assert probe[0]["timestamp"] < finished_at < probe[-1]["timestamp"] - 5
                assert child_result["passed"] and child_result["elapsed"] >= 90
                result["child_probe"] = {**child_result, "mailbox_epochs": sorted(epochs),
                    "seconds_after_initial_final": round(probe[-1]["timestamp"] - finished_at, 3)}
            for key, record in records.items():
                (out / f"cold-{key}.json").write_text(json.dumps(record.to_dict(), ensure_ascii=False), encoding="utf-8")
            result.update(passed=True, verification=verified, completed_reviewers=sorted(completed_reviewers), events=len(client.events),
                tool_calls=sum(e.get("type") == "tool_call" for e in client.events),
                subagent_events=sum(str(e.get("type", "")).startswith("subagent.") for e in client.events))
        except BaseException as error:
            result["error"] = f"{type(error).__name__}: {error}".replace(config.llm.api_key, "[redacted]")
            raise
        finally:
            try:
                if client.ws is not None:
                    await client.ws.close()
                    await asyncio.gather(client.reader, return_exceptions=True)
                if process.poll() is None:
                    if backend_ready:
                        async with httpx.AsyncClient(trust_env=False) as http:
                            response = await http.post(f"http://127.0.0.1:{port}/api/runtime/shutdown")
                            response.raise_for_status()
                            assert response.json()["accepted"] is True
                    else:
                        process.terminate()
                result["shutdown_exit_code"] = await asyncio.to_thread(process.wait, 30)
                assert not backend_ready or result["shutdown_exit_code"] == 0
            except Exception as error:
                result.update(passed=False, shutdown_error=f"{type(error).__name__}: {error}".replace(config.llm.api_key, "[redacted]"))
                raise
            else:
                result["source_unchanged_during_run"] = backend_source_snapshot() == source_snapshot
                if not result["source_unchanged_during_run"]:
                    result.update(passed=False, source_error="Backend source changed during this acceptance run")
                assert result["source_unchanged_during_run"], result["source_error"]
            finally:
                result["elapsed_seconds"] = round(time.monotonic() - started, 3)
                (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--configured-state", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=100)
    parser.add_argument("--child-probe", action="store_true", help="Require a real child final/follow-up with a live process in between.")
    args = parser.parse_args()
    os.environ["MINICODE_STATE_ROOT"] = str(args.configured_state.resolve())
    from backend.config import load_config
    config = load_config(cwd=None)
    if not config.llm.api_key:
        parser.error("The selected provider has no credential")
    asyncio.run(exercise(args, config))


if __name__ == "__main__":
    main()
