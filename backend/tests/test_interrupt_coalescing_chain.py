from __future__ import annotations

import asyncio
from functools import partial
import threading
import time

import pytest
from fastapi.testclient import TestClient

from backend.api import _state
from backend.main import app
from tests.test_conversations import _install_noop_llm, _receive_until_event


@pytest.mark.timeout(60)
def test_same_turn_interrupts_share_one_worker_without_blocking_approval(tmp_path, monkeypatch):
    _install_noop_llm(monkeypatch)
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    gate, entered = threading.Event(), threading.Event()
    calls = []
    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=interrupt-group-test") as websocket:
            _receive_until_event(websocket, "llm.model.updated")
            session = _state.ws_manager.get_session("interrupt-group-test")
            owner = session.conversation_repo.create_conversation(title="Interrupt fixture")
            session.active_conversation_id = owner.id
            session._conversation_streams[owner.id] = {"turn_id": "turn-fixture", "message_id": "message-fixture", "terminal_fenced": False}
            session.run_manager.run_task_ids[owner.id] = "task-fixture"
            session.command_dispatcher.max_command_tasks = 2

            async def slow_cancel(**kwargs):
                if kwargs.get("reason") != "user_interrupted":
                    return
                calls.append(kwargs["conversation_id"])
                entered.set()
                await asyncio.to_thread(gate.wait, 20)
                session._conversation_streams[owner.id]["terminal_fenced"] = True

            monkeypatch.setattr(session, "cancel_agent_runs", slow_cancel)
            ids = {f"stop-{index}" for index in range(24)}
            try:
                for command_id in sorted(ids):
                    websocket.send_json({"type": "interrupt", "conversation_id": owner.id, "turn_id": "turn-fixture",
                                         "message_id": "message-fixture", "client_command_id": command_id})
                acknowledged = set()
                while acknowledged != ids:
                    payload = websocket.receive_json()
                    if payload.get("type") == "client.command.ack" and payload.get("client_command_id") in ids:
                        acknowledged.add(payload["client_command_id"])
                assert entered.wait(5)
                assert len(calls) == 1 and len(session.command_dispatcher._interrupt_groups) == 1
                assert len(session.command_dispatcher.command_tasks) == 1

                websocket.send_json({"type": "control_response", "request_id": "missing", "response": {},
                                     "client_command_id": "approval-fixture"})
                approval = None
                while approval is None:
                    payload = websocket.receive_json()
                    if payload.get("type") == "command.result" and payload.get("client_command_id") == "approval-fixture":
                        approval = payload
                assert approval["level"] == "error" and not gate.is_set()

                gate.set()
                results = {}
                while set(results) != ids:
                    payload = websocket.receive_json()
                    if payload.get("type") == "command.result" and payload.get("client_command_id") in ids:
                        results[payload["client_command_id"]] = payload
                assert all(item["level"] == "success" for item in results.values())
                assert calls == [owner.id]
                deadline = time.monotonic() + 5
                while session.command_dispatcher._interrupt_groups and time.monotonic() < deadline:
                    time.sleep(.02)
                assert not session.command_dispatcher._interrupt_groups
            finally:
                gate.set()


@pytest.mark.timeout(45)
def test_different_interrupt_targets_can_stop_independently(tmp_path, monkeypatch):
    _install_noop_llm(monkeypatch)
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=interrupt-independent-test") as websocket:
            _receive_until_event(websocket, "llm.model.updated")
            session = _state.ws_manager.get_session("interrupt-independent-test")
            owners = [session.conversation_repo.create_conversation(title="Independent stop") for _ in range(2)]
            session.active_conversation_id = owners[0].id
            gates = {owner.id: threading.Event() for owner in owners}
            entered = {owner.id: threading.Event() for owner in owners}
            for index, owner in enumerate(owners):
                session._conversation_streams[owner.id] = {"turn_id": f"turn-{index}", "message_id": f"message-{index}", "terminal_fenced": False}

            async def slow_cancel(**kwargs):
                if kwargs.get("reason") != "user_interrupted":
                    return
                owner_id = kwargs["conversation_id"]
                entered[owner_id].set()
                await asyncio.to_thread(gates[owner_id].wait, 15)
                session._conversation_streams[owner_id]["terminal_fenced"] = True

            monkeypatch.setattr(session, "cancel_agent_runs", slow_cancel)
            try:
                for index, owner in enumerate(owners):
                    websocket.send_json({"type": "interrupt", "conversation_id": owner.id, "turn_id": f"turn-{index}",
                                         "message_id": f"message-{index}", "client_command_id": f"independent-{index}"})
                    assert _receive_until_event(websocket, "client.command.ack")["client_command_id"] == f"independent-{index}"
                assert all(event.wait(5) for event in entered.values())
                gates[owners[1].id].set()
                result = _receive_until_event(websocket, "command.result")
                assert result["client_command_id"] == "independent-1" and result["level"] == "success"
                assert not gates[owners[0].id].is_set()
                gates[owners[0].id].set()
                result = _receive_until_event(websocket, "command.result")
                assert result["client_command_id"] == "independent-0" and result["level"] == "success"
            finally:
                for gate in gates.values():
                    gate.set()


@pytest.mark.timeout(45)
def test_group_continues_after_one_completion_write_fails(tmp_path, monkeypatch):
    _install_noop_llm(monkeypatch)
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    gate, entered, second_completed = threading.Event(), threading.Event(), threading.Event()
    calls = []
    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=interrupt-group-write-test") as websocket:
            _receive_until_event(websocket, "llm.model.updated")
            session = _state.ws_manager.get_session("interrupt-group-write-test")
            owner = session.conversation_repo.create_conversation(title="Interrupt write fixture")
            session.active_conversation_id = owner.id
            session._conversation_streams[owner.id] = {"turn_id": "turn-fixture", "message_id": "message-fixture", "terminal_fenced": False}
            queue = session.run_manager.durable_client_commands
            complete = queue.complete_client_command
            failed = False

            def fail_first_completion(command_id):
                nonlocal failed
                if command_id == "stop-first" and not failed:
                    failed = True
                    raise OSError("controlled completion write failure")
                result = complete(command_id)
                if command_id == "stop-second":
                    second_completed.set()
                return result

            async def slow_cancel(**kwargs):
                if kwargs.get("reason") != "user_interrupted":
                    return
                calls.append(kwargs["conversation_id"])
                entered.set()
                await asyncio.to_thread(gate.wait, 15)
                session._conversation_streams[owner.id]["terminal_fenced"] = True

            monkeypatch.setattr(queue, "complete_client_command", fail_first_completion)
            monkeypatch.setattr(session, "cancel_agent_runs", slow_cancel)
            try:
                for command_id in ("stop-first", "stop-second"):
                    websocket.send_json({"type": "interrupt", "conversation_id": owner.id, "turn_id": "turn-fixture",
                                         "message_id": "message-fixture", "client_command_id": command_id})
                    ack = _receive_until_event(websocket, "client.command.ack")
                    assert ack["client_command_id"] == command_id
                assert entered.wait(5)
                gate.set()
                assert second_completed.wait(10)
                results = {}
                while len(results) < 2:
                    payload = websocket.receive_json()
                    if payload.get("type") == "command.result" and payload.get("client_command_id") in {"stop-first", "stop-second"}:
                        results[payload["client_command_id"]] = payload
                assert all(result["level"] == "success" for result in results.values())
                assert calls == [owner.id]
                assert queue.has_client_command("stop-first")
                websocket.send_json({"type": "session.restore", "last_conversation_id": owner.id})
                _receive_until_event(websocket, "session.restored")
                deadline = time.monotonic() + 10
                while not queue.is_client_command_completed("stop-first") and time.monotonic() < deadline:
                    time.sleep(.02)
                assert queue.is_client_command_completed("stop-first")
                assert calls == [owner.id]
            finally:
                gate.set()


@pytest.mark.timeout(45)
def test_group_finishing_after_restore_delivers_original_results(tmp_path, monkeypatch):
    _install_noop_llm(monkeypatch)
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    entered, release, restored, delivered, first_replay, release_replay = (threading.Event() for _ in range(6))
    received = []
    ids = {"reconnect-stop-one", "reconnect-stop-two"}
    with TestClient(app) as client:
        try:
            with client.websocket_connect("/ws?session_id=interrupt-reconnect-test") as websocket:
                _receive_until_event(websocket, "llm.model.updated")
                session = _state.ws_manager.get_session("interrupt-reconnect-test")
                owner = session.conversation_repo.create_conversation(title="Interrupt reconnect fixture")
                session.active_conversation_id = owner.id
                session._conversation_streams[owner.id] = {"turn_id": "turn-fixture", "message_id": "message-fixture", "terminal_fenced": False}

                async def slow_cancel(**kwargs):
                    if kwargs.get("reason") != "user_interrupted":
                        return
                    entered.set()
                    await asyncio.to_thread(release.wait, 20)
                    session._conversation_streams[owner.id]["terminal_fenced"] = True

                monkeypatch.setattr(session, "cancel_agent_runs", slow_cancel)
                for command_id in sorted(ids):
                    websocket.send_json({"type": "interrupt", "conversation_id": owner.id, "turn_id": "turn-fixture",
                                         "message_id": "message-fixture", "client_command_id": command_id})
                    ack = _receive_until_event(websocket, "client.command.ack")
                    assert ack["client_command_id"] == command_id
                assert entered.wait(5)
            replay = session.command_dispatcher._replay_pending_client_commands

            async def observe_restore(generation, *, replay_results=True):
                await replay(generation, replay_results=replay_results)
                if replay_results:
                    restored.set()

            monkeypatch.setattr(session.command_dispatcher, "_replay_pending_client_commands", observe_restore)
            with client.websocket_connect("/ws?session_id=interrupt-reconnect-test") as websocket:
                _receive_until_event(websocket, "llm.model.updated")
                send_json = session.ws.send_json

                async def observe_delivery(payload):
                    if payload.get("type") == "command.result" and payload.get("client_command_id") in ids and not first_replay.is_set():
                        first_replay.set()
                        await asyncio.to_thread(release_replay.wait, 15)
                    await send_json(payload)
                    if payload.get("type") == "command.result" and payload.get("client_command_id") in ids:
                        received.append(payload["client_command_id"])
                        if set(received) == ids:
                            delivered.set()

                monkeypatch.setattr(session.ws, "send_json", observe_delivery)
                websocket.send_json({"type": "session.restore", "last_conversation_id": owner.id})
                assert restored.wait(10)
                release.set()
                assert first_replay.wait(10)
                queue = session.run_manager.durable_client_commands
                deadline = time.monotonic() + 10
                while not all(queue.is_client_command_completed(command_id) for command_id in ids) and time.monotonic() < deadline:
                    time.sleep(.02)
                assert all(queue.is_client_command_completed(command_id) for command_id in ids)
                release_replay.set()
                assert delivered.wait(10)
                results = {}
                while set(results) != ids:
                    payload = websocket.receive_json()
                    if payload.get("type") == "command.result" and payload.get("client_command_id") in ids:
                        results[payload["client_command_id"]] = payload
                assert all(result["level"] == "success" for result in results.values())
                assert len(received) == len(ids)
        finally:
            release.set()
            release_replay.set()


@pytest.mark.timeout(45)
def test_cancelled_group_replays_each_accepted_interrupt(tmp_path, monkeypatch):
    _install_noop_llm(monkeypatch)
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    entered, release = threading.Event(), threading.Event()
    ids = {"cancel-group-one", "cancel-group-two"}
    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=interrupt-group-cancel-test") as websocket:
            _receive_until_event(websocket, "llm.model.updated")
            session = _state.ws_manager.get_session("interrupt-group-cancel-test")
            owner = session.conversation_repo.create_conversation(title="Interrupt group cancellation")
            session.active_conversation_id = owner.id
            session._conversation_streams[owner.id] = {"turn_id": "turn-fixture", "message_id": "message-fixture", "terminal_fenced": False}

            async def slow_cancel(**kwargs):
                if kwargs.get("reason") != "user_interrupted":
                    return
                entered.set()
                await asyncio.to_thread(release.wait, 15)
                session._conversation_streams[owner.id]["terminal_fenced"] = True

            async def cancel_group():
                tasks = tuple(session.command_dispatcher.command_tasks)
                assert len(tasks) == 1
                tasks[0].cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

            monkeypatch.setattr(session, "cancel_agent_runs", slow_cancel)
            try:
                for command_id in sorted(ids):
                    websocket.send_json({"type": "interrupt", "conversation_id": owner.id, "turn_id": "turn-fixture",
                                         "message_id": "message-fixture", "client_command_id": command_id})
                    ack = _receive_until_event(websocket, "client.command.ack")
                    assert ack["client_command_id"] == command_id
                assert entered.wait(5)
                client.portal.call(cancel_group)
                assert not session.command_dispatcher.active_client_command_ids
                queue = session.run_manager.durable_client_commands
                assert {command.data["client_command_id"] for command in queue.pending_client_commands()} == ids
                release.set()
                websocket.send_json({"type": "session.restore", "last_conversation_id": owner.id})
                results = {}
                while set(results) != ids:
                    payload = websocket.receive_json()
                    if payload.get("type") == "command.result" and payload.get("client_command_id") in ids:
                        results[payload["client_command_id"]] = payload
                assert all(result["level"] == "success" for result in results.values())
                deadline = time.monotonic() + 5
                while not all(queue.is_client_command_completed(command_id) for command_id in ids) and time.monotonic() < deadline:
                    time.sleep(.02)
                assert all(queue.is_client_command_completed(command_id) for command_id in ids)
            finally:
                release.set()


@pytest.mark.timeout(60)
def test_interrupt_result_waits_for_real_run_after_cancel_deadline(tmp_path, monkeypatch):
    from backend.workspace.trust import trust_created_task_directory

    _install_noop_llm(monkeypatch)
    trust_created_task_directory(tmp_path)
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.run_manager.CANCELLATION_DRAIN_TIMEOUT_SECONDS", .05)
    entered, cancelled, release, result_sent = (threading.Event() for _ in range(4))
    seen = []
    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=interrupt-terminal-test") as websocket:
            _receive_until_event(websocket, "llm.model.updated")
            session = _state.ws_manager.get_session("interrupt-terminal-test")
            owner = session.conversation_repo.create_conversation(title="Real run fixture", workspace_root=str(tmp_path),
                                                                   permission_mode="bypass", memory_mode="none")
            session.active_conversation_id = owner.id
            send_event = session.send_event

            async def capture_event(event):
                await send_event(event)
                if event.type == "done" and event.data.get("conversation_id") == owner.id:
                    seen.append("done")
                if event.type == "command.result" and event.data.get("command") == "interrupt":
                    seen.append("interrupt-result")
                    result_sent.set()

            async def slow_turn(*_args, **_kwargs):
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    await asyncio.to_thread(release.wait, 20)
                    raise

            monkeypatch.setattr(session, "send_event", capture_event)
            monkeypatch.setattr(session, "_run_agent_locked", slow_turn)
            try:
                start = client.portal.start_task_soon(partial(session.start_agent_run, "A real running turn",
                    conversation_id=owner.id, metadata={"assistant_message_id": "slow-answer", "user_message_id": "slow-input"}))
                assert entered.wait(15), start.exception() if start.done() else "run did not reach its execution body"
                run_context = session.run_manager.context_for(owner.id)
                assert run_context is not None and not run_context.turn_terminal_receipt.done()
                websocket.send_json({"type": "interrupt", "conversation_id": owner.id, "message_id": "slow-answer",
                                     "client_command_id": "slow-stop"})
                assert cancelled.wait(10)
                assert not result_sent.wait(.5)
                assert not run_context.turn_terminal_receipt.done()
                release.set()
                assert result_sent.wait(15)
                assert seen.index("done") < seen.index("interrupt-result")
                assert run_context.turn_terminal_receipt.result()["status"] == "cancelled"
                result = None
                while result is None:
                    payload = websocket.receive_json()
                    if payload.get("type") == "command.result" and payload.get("client_command_id") == "slow-stop":
                        result = payload
                assert result["level"] == "success"
            finally:
                release.set()
