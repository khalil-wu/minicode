import asyncio
import json
from queue import Empty
from collections.abc import AsyncIterator
from types import SimpleNamespace
from fastapi.testclient import TestClient

from backend.agent.loop import run_agent_loop
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.attachments.store import AttachmentStore
from backend.config import PROJECT_ROOT, AgentSettings, LLMSettings, PermissionSettings, TokenBudget, load_llm_settings
from backend.llm.base import LLMAdapter, LLMMessage, StreamEvent, StreamEventType
from backend.tools.agent_tools import ReadArtifactTool
from backend.llm.openai_adapter import OpenAIAdapter, _clean_error_message
from backend.agent.message import AgentEvent
from backend.conversations.repository import ConversationRepository
from backend.main import app
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.agent.tool_execution import generate_diff as _generate_diff
from backend.tools.base import PermissionLevel, ToolResult
from backend.tools.registry import ToolRegistry
from backend.ws.handler import _build_effective_user_message


def _receive_json(ws, *, timeout: float = 5.0) -> dict[str, object]:
    send_queue = getattr(ws, "_send_queue", None)
    raise_on_close = getattr(ws, "_raise_on_close", None)
    if send_queue is None:
        return ws.receive_json()
    try:
        message = send_queue.get(timeout=timeout)
    except Empty as exc:
        raise AssertionError(f"timed out waiting for websocket event after {timeout:.1f}s") from exc
    if isinstance(message, BaseException):
        raise message
    if callable(raise_on_close):
        raise_on_close(message)
    if "text" in message:
        return json.loads(message["text"])
    if "bytes" in message:
        return json.loads(message["bytes"].decode("utf-8"))
    raise AssertionError(f"websocket message did not contain JSON payload: {message!r}")


def _receive_next_non_task_update(ws, *, max_attempts: int = 20) -> dict[str, object]:
    bookkeeping_events = {
        "task.update",
        "file.changed",
        "session.state_changed",
        "agent.run.started",
        "agent.run.completed",
    }
    for _ in range(max_attempts):
        payload = _receive_json(ws)
        if payload.get("type") in bookkeeping_events:
            continue
        return payload
    raise AssertionError("did not receive a non task.update/file.changed websocket event in time")


def _receive_next_type(ws, event_type: str, *, max_attempts: int = 20) -> dict[str, object]:
    for _ in range(max_attempts):
        payload = _receive_next_non_task_update(ws)
        if payload.get("type") == event_type:
            return payload
    raise AssertionError(f"did not receive websocket event type {event_type!r} in time")


def _receive_control_request(ws, request_id: str, *, max_attempts: int = 20) -> dict[str, object]:
    """Wait for the ``control_request`` carrying ``request_id``.

    Approvals and elicitations share one wire type, so the request id is what
    distinguishes them.
    """
    for _ in range(max_attempts):
        payload = _receive_next_non_task_update(ws)
        if payload.get("type") == "control_request" and payload.get("request_id") == request_id:
            return payload
    raise AssertionError(f"did not receive control_request {request_id!r} in time")


def _assert_event_envelope(payload: dict[str, object]) -> None:
    assert isinstance(payload.get("seq"), int)
    assert isinstance(payload.get("event_id"), str) and payload["event_id"]


def _assert_startup_events(ws) -> list[dict[str, object]]:
    events = [
        _receive_next_non_task_update(ws),
        _receive_next_non_task_update(ws),
    ]
    assert {event["type"] for event in events} == {"mcp_status", "llm.model.updated"}
    return events


def _create_active_conversation(ws, *, title: str = "Smoke test chat") -> dict[str, object]:
    ws.send_json({"type": "conversation.create", "title": title, "memory_mode": "none"})
    listing = _receive_next_type(ws, "conversation.list")
    assert listing["active_conversation_id"]
    assert listing["active_conversation"]["id"] == listing["active_conversation_id"]
    result = _receive_next_type(ws, "command.result")
    assert result["command"] == "conversation.create"
    assert result["data"]["conversation_id"] == listing["active_conversation_id"]
    return listing


class _ModelSwitchLLM:
    def __init__(self, model: str) -> None:
        self.model = model

    def apply_reasoning_policy(self, _policy) -> None:
        return None


def _fake_llm_factory(config, model_override=None):
    selected = model_override or getattr(config.llm, "model", "")
    return _ModelSwitchLLM(selected)


def _install_llm_factory(monkeypatch, factory=_fake_llm_factory) -> None:
    def canonical_factory(config, model_override=None, **_kwargs):
        return factory(config, model_override=model_override)

    monkeypatch.setattr("backend.main._create_session_llm", canonical_factory)
    monkeypatch.setattr("backend.llm.model_registry.create_session_llm", canonical_factory)


async def _admit_fake_turn(kwargs) -> None:
    context = kwargs["context_builder"]
    history_start = context.history_length
    context.append_user(kwargs["user_message"])
    await kwargs["metadata"]["commit_turn_admission"](
        boundary_input=SimpleNamespace(consumed_steer=None),
        history_start=history_start,
        history_end=context.history_length,
    )


async def _fake_control_request_loop(*args, **kwargs):
    await _admit_fake_turn(kwargs)
    yield AgentEvent.approval_request(
        tool_call_id="tool_confirm_1",
        tool_name="write_file",
        args={"file_path": "demo.txt"},
    )
    yield AgentEvent(type="ask_user", data={"tool_call_id": "ask_1", "question": "Do you want to proceed?"})
    # QueryEngine owns canonical terminal validation; a durable successful
    # turn must include an accepted final answer before ``done``.
    yield AgentEvent.agent_message_completed("stub control-request reply", source="model_final")
    yield AgentEvent.done(input_tokens=1, output_tokens=1)


async def _fake_agent_loop(*args, **kwargs):
    await _admit_fake_turn(kwargs)
    yield AgentEvent.agent_message_completed("stub reply", source="model_final")
    yield AgentEvent.done(input_tokens=3, output_tokens=2)


async def _slow_agent_loop(*args, **kwargs):
    await _admit_fake_turn(kwargs)
    yield AgentEvent.agent_message_completed("started")
    await asyncio.sleep(10)
    yield AgentEvent.done(input_tokens=1, output_tokens=1)


async def _steerable_agent_loop(*args, **kwargs):
    await _admit_fake_turn(kwargs)
    metadata = kwargs.get("metadata") or {}
    run_context = kwargs.get("run_context")
    queue = (
        getattr(run_context, "turn_input_queue", None)
        or metadata.get("turn_input_queue")
    )
    persist = (
        getattr(run_context, "persist_consumed_turn_input", None)
        or metadata.get("persist_consumed_turn_input")
    )
    yield AgentEvent.agent_item(id="started", kind="process_text", content="started", source="model_narration")
    for _ in range(200):
        item = queue.pop_steer() if queue is not None else None
        if item is not None:
            if callable(persist):
                persisted = persist(item)
                if asyncio.iscoroutine(persisted):
                    await persisted
            yield AgentEvent.agent_message_completed(
                f"steered:{item.content}",
                source="model_final",
            )
            yield AgentEvent.done(input_tokens=2, output_tokens=2)
            return
        await asyncio.sleep(0.01)
    yield AgentEvent.error("steer was not delivered to the active turn")
    yield AgentEvent.done(status="failed", reason="steer_timeout")


def test_websocket_ping_round_trip() -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_ping") as ws:
            _assert_startup_events(ws)

            ws.send_json({"type": "ping"})
            pong = _receive_next_non_task_update(ws)
            assert pong["type"] == "pong"
            _assert_event_envelope(pong)


def test_websocket_acknowledges_client_command_id() -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_command_ack") as ws:
            _assert_startup_events(ws)

            ws.send_json({"type": "ping", "client_command_id": "cmd_test_ping"})
            ack = _receive_next_type(ws, "client.command.ack")
            assert ack["client_command_id"] == "cmd_test_ping"
            assert ack["command_type"] == "ping"
            _assert_event_envelope(ack)

            pong = _receive_next_type(ws, "pong")
            assert pong["type"] == "pong"


def test_websocket_accepts_control_protocol_messages() -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_control_protocol") as ws:
            _assert_startup_events(ws)

            ws.send_json(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": "req_missing",
                        "response": {"action": "accept"},
                    },
                }
            )
            ws.send_json({"type": "control_cancel_request", "request_id": "req_missing"})

            ws.send_json({"type": "ping"})
            pong = _receive_next_non_task_update(ws)
            assert pong["type"] == "pong"
            _assert_event_envelope(pong)


def test_websocket_permission_mode_switch_updates_runtime_snapshot() -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_permission_mode") as ws:
            _assert_startup_events(ws)

            ws.send_json(
                {
                    "type": "conversation.permission_mode.set",
                    "mode": "plan",
                    "source": "test-suite",
                }
            )

            mode_event = None
            runtime_mode = None
            for _ in range(10):
                payload = _receive_json(ws)
                if payload.get("type") == "permission.mode.updated":
                    mode_event = payload
                if payload.get("type") == "task.update":
                    runtime_mode = payload.get("session", {}).get("permission_mode")
                if mode_event is not None and runtime_mode == "plan":
                    break

    assert mode_event is not None
    assert mode_event["mode"] == "plan"
    assert mode_event["source"] == "test-suite"
    assert runtime_mode == "plan"


def test_websocket_permission_rules_round_trip() -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_permission_rules") as ws:
            _assert_startup_events(ws)
            _create_active_conversation(ws, title="Permission rules smoke")

            ws.send_json(
                {
                    "type": "conversation.permission.rules.add",
                    "rule_kind": "deny",
                    "pattern": "run_*",
                    "source": "test-suite",
                }
            )
            added = None
            for _ in range(12):
                payload = _receive_json(ws)
                if payload.get("type") == "task.update":
                    continue
                if payload.get("type") == "permission.rules.updated":
                    added = payload
                    break

            assert added is not None
            assert any(item["pattern"] == "run_*" for item in added["rules"]["session_deny"])

            ws.send_json({"type": "conversation.permission.rules.list"})
            listed = None
            for _ in range(12):
                payload = _receive_json(ws)
                if payload.get("type") == "task.update":
                    continue
                if payload.get("type") == "permission.rules.updated":
                    listed = payload
                    break

            assert listed is not None
            assert any(item["pattern"] == "run_*" for item in listed["rules"]["session_deny"])

            ws.send_json(
                {
                    "type": "conversation.permission.rules.remove",
                    "rule_kind": "deny",
                    "pattern": "run_*",
                    "source": "test-suite",
                }
            )
            removed = None
            for _ in range(12):
                payload = _receive_json(ws)
                if payload.get("type") == "task.update":
                    continue
                if payload.get("type") == "permission.rules.updated":
                    removed = payload
                    break

            assert removed is not None
            assert all(item["pattern"] != "run_*" for item in removed["rules"]["session_deny"])


def test_websocket_runtime_inspect_commandsemit_command_result() -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_runtime_inspect") as ws:
            _assert_startup_events(ws)

            ws.send_json({"type": "session.tasks.inspect"})
            tasks_result = _receive_next_non_task_update(ws)
            assert tasks_result["type"] == "command.result"
            assert tasks_result["command"] == "tasks"
            assert "Current session tasks" in tasks_result["message"]

            ws.send_json({"type": "session.status.inspect"})
            status_result = _receive_next_non_task_update(ws)
            assert status_result["type"] == "command.result"
            assert status_result["command"] == "status"
            assert "Runtime status" in status_result["message"]

            ws.send_json({"type": "session.permissions.inspect"})
            permissions_result = _receive_next_non_task_update(ws)
            assert permissions_result["type"] == "command.result"
            assert permissions_result["command"] == "permissions"
            assert "Permission mode" in permissions_result["message"]

            ws.send_json({"type": "runtime.capabilities.inspect"})
            capabilities = _receive_next_type(ws, "runtime.capabilities")
            assert capabilities["session_id"] == "session_test_runtime_inspect"
            assert capabilities["capabilities"]["summary"]["tools_total"] >= 1
            assert isinstance(capabilities["capabilities"]["tool_views"], list)
            assert isinstance(capabilities["capabilities"]["tools"], list)
            assert capabilities["capabilities"]["permission"]["profile"] == "confirm"
            assert "sandbox_status" in capabilities["capabilities"]["permission"]


def test_websocket_conversation_goal_command_round_trip() -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_goal_command") as ws:
            _assert_startup_events(ws)
            _create_active_conversation(ws, title="Goal command smoke")

            ws.send_json({
                "type": "conversation.goal.set",
                "text": "对标 Codex 桌面端",
            })

            updated = _receive_next_non_task_update(ws)
            assert updated["type"] == "goal.updated"
            assert updated["goal"]["text"] == "对标 Codex 桌面端"
            assert updated["goal"]["status"] == "active"

            listing = _receive_next_non_task_update(ws)
            assert listing["type"] == "conversation.list"
            assert listing["active_conversation"]["goal"]["text"] == "对标 Codex 桌面端"

            result = _receive_next_non_task_update(ws)
            assert result["type"] == "command.result"
            assert result["command"] == "goal"

            conversation_id = listing["active_conversation_id"]
            ws.send_json({
                "type": "conversation.goal.set",
                "conversation_id": conversation_id,
                "action": "pause",
            })
            paused = _receive_next_type(ws, "goal.updated")
            assert paused["goal"]["status"] == "paused"

            ws.send_json({
                "type": "conversation.goal.set",
                "conversation_id": conversation_id,
                "action": "clear",
            })
            cleared = _receive_next_type(ws, "goal.updated")
            assert cleared["goal"] == {}


def test_websocket_emits_control_requests_for_approvals_and_elicitations(monkeypatch) -> None:
    _install_llm_factory(monkeypatch)
    monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", _fake_control_request_loop)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_control_outbound") as ws:
            _assert_startup_events(ws)

            ws.send_json({"type": "user_message", "content": "trigger control request"})

            approval = _receive_next_non_task_update(ws)
            ask = _receive_next_non_task_update(ws)

    assert approval["type"] == "control_request"
    assert approval["request_id"] == "tool_confirm_1"
    assert approval["request"]["subtype"] == "can_use_tool"
    assert approval["request"]["tool_use_id"] == "tool_confirm_1"

    assert ask["type"] == "control_request"
    assert ask["request_id"] == "ask_1"
    assert ask["request"]["subtype"] == "elicitation"


def test_conversation_switch_reemits_pending_prompts_for_target_conversation(monkeypatch) -> None:
    _install_llm_factory(monkeypatch)
    monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", _fake_control_request_loop)

    repo = ConversationRepository()
    other = repo.create_conversation(title="Other pending switch target")

    try:
        with TestClient(app) as client:
            with client.websocket_connect("/ws?session_id=session_test_switch_pending_reemit") as ws:
                _assert_startup_events(ws)

                active_id = _create_active_conversation(ws, title="Pending prompt active conversation")["active_conversation_id"]

                ws.send_json({"type": "user_message", "content": "trigger pending prompts"})
                assert _receive_control_request(ws, "tool_confirm_1")["request"]["subtype"] == "can_use_tool"
                assert _receive_control_request(ws, "ask_1")["request"]["subtype"] == "elicitation"
                assert _receive_next_type(ws, "done")["type"] == "done"

                ws.send_json({"type": "conversation.switch", "conversation_id": other.id})
                assert _receive_next_type(ws, "conversation.switched")["conversation_id"] == other.id

                ws.send_json({"type": "conversation.switch", "conversation_id": active_id})
                switched = _receive_next_type(ws, "conversation.switched")
                reemitted_approval = _receive_control_request(ws, "tool_confirm_1")
                reemitted_ask = _receive_control_request(ws, "ask_1")

        assert switched["conversation_id"] == active_id
        assert reemitted_approval["conversation_id"] == active_id
        assert reemitted_approval["request"]["tool_use_id"] == "tool_confirm_1"
        assert reemitted_ask["conversation_id"] == active_id
        assert reemitted_ask["request"]["tool_use_id"] == "ask_1"
    finally:
        repo.delete_conversation(other.id)


def test_websocket_restore_reemits_control_ask_user_pending_state(monkeypatch) -> None:
    _install_llm_factory(monkeypatch)
    monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", _fake_control_request_loop)

    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws?session_id=session_test_control_ask_user_restore"
        ) as ws:
            _assert_startup_events(ws)

            ws.send_json({"type": "user_message", "content": "trigger control ask user"})
            assert _receive_next_type(ws, "control_request")["request_id"] == "tool_confirm_1"
            assert _receive_next_type(ws, "control_request")["request_id"] == "ask_1"
            assert _receive_next_type(ws, "done")["type"] == "done"

            ws.send_json({"type": "session.restore"})
            restored = _receive_next_type(ws, "session.restored")
            reemitted_approval = _receive_next_type(ws, "control_request")
            reemitted_ask = _receive_next_type(ws, "control_request")

    assert restored["type"] == "session.restored"
    assert restored["session"]["pending_approval_count"] == 2
    assert [item["request_id"] for item in restored["session"]["pending_approvals"]] == [
        "tool_confirm_1",
        "ask_1",
    ]
    assert reemitted_approval["request_id"] == "tool_confirm_1"
    assert reemitted_approval["request"]["subtype"] == "can_use_tool"
    assert reemitted_ask["request_id"] == "ask_1"
    assert reemitted_ask["request"]["subtype"] == "elicitation"


def test_status_runtime_snapshot_aggregates_without_cross_session_payloads(monkeypatch) -> None:
    _install_llm_factory(monkeypatch)
    monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", _fake_control_request_loop)

    session_id = "session_test_pending_prompt_status"
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws?session_id={session_id}") as ws:
            _assert_startup_events(ws)

            ws.send_json({"type": "user_message", "content": "trigger pending prompts"})
            assert _receive_control_request(ws, "tool_confirm_1")["request"]["subtype"] == "can_use_tool"
            assert _receive_control_request(ws, "ask_1")["request"]["subtype"] == "elicitation"
            assert _receive_next_type(ws, "done")["type"] == "done"

            runtime = client.get("/api/status").json()["runtime"]

    assert runtime["active_sessions"] == 1
    assert set(runtime) == {
        "active_sessions",
        "running_tasks",
        "pending_tasks",
        "completed_tasks",
        "failed_tasks",
        "cancelled_tasks",
    }
    serialized = json.dumps(runtime, sort_keys=True)
    assert session_id not in serialized
    assert "tool_confirm_1" not in serialized
    assert "ask_1" not in serialized


def test_websocket_control_protocol_forwards_structured_approval_diff(monkeypatch) -> None:
    _install_llm_factory(monkeypatch)

    async def _fake_structured_control_request_loop(*args, **kwargs) -> AsyncIterator[AgentEvent]:
        await _admit_fake_turn(kwargs)
        yield AgentEvent.approval_request(
            tool_call_id="tool_confirm_structured",
            tool_name="apply_patch",
            args={"file_path": "demo.txt"},
            diff={
                "format": "structured",
                "stats": {
                    "files_count": 1,
                    "additions": 4,
                    "deletions": 1,
                },
                "files": [
                    {
                        "path": "demo.txt",
                        "status": "modified",
                        "additions": 4,
                        "deletions": 1,
                        "patch": "@@ -1 +1 @@\n-before\n+after",
                    }
                ],
            },
        )
        yield AgentEvent.done(input_tokens=1, output_tokens=1)

    monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", _fake_structured_control_request_loop)

    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws?session_id=session_test_control_structured"
        ) as ws:
            _assert_startup_events(ws)

            ws.send_json({
                "type": "conversation.permission_mode.set",
                "mode": "confirm",
                "source": "test-suite",
            })
            for _ in range(10):
                payload = _receive_json(ws)
                if payload.get("type") == "permission.mode.updated" and payload.get("mode") == "confirm":
                    break

            ws.send_json({"type": "user_message", "content": "trigger structured control request"})

            approval = None
            for _ in range(10):
                payload = _receive_json(ws)
                if payload.get("type") == "control_request":
                    approval = payload
                    break

    assert approval is not None
    assert approval["type"] == "control_request"
    assert approval["request_id"] == "tool_confirm_structured"
    assert approval["request"]["diff"]["format"] == "structured"
    assert approval["request"]["diff"]["files"][0]["path"] == "demo.txt"


def test_generate_diff_returns_structured_payload_for_write_file(tmp_path) -> None:
    file_path = tmp_path / "demo.txt"
    file_path.write_text("before\n", encoding="utf-8")

    diff = _generate_diff(
        "write_file",
        {
            "file_path": str(file_path),
            "content": "after\nextra\n",
        },
    )

    assert diff is not None
    assert diff["format"] == "structured"
    assert diff["stats"] == {
        "files_count": 1,
        "additions": 2,
        "deletions": 1,
    }
    assert diff["files"][0]["path"] == str(file_path)
    assert diff["files"][0]["status"] == "modified"
    assert diff["files"][0]["size_bytes"] == len("after\nextra\n".encode("utf-8"))
    assert "@@ -1 +1,2 @@" in diff["files"][0]["patch"]


def test_write_file_requires_canonical_file_path(tmp_path) -> None:
    from backend.permissions.context import PermissionContext, ToolExecutionContext
    from backend.tools.file_tools import WriteFileTool

    alias_result = asyncio.run(
        WriteFileTool().execute(
            {"path": "alias.html", "content": "<html>ok</html>", "expected_hash": ""},
            context=ToolExecutionContext(permission=PermissionContext(), workspace_root=tmp_path),
        )
    )
    target = tmp_path / "canonical.html"
    canonical_result = asyncio.run(
        WriteFileTool().execute(
            {
                "file_path": "canonical.html",
                "content": "<html>ok</html>",
                "expected_hash": "",
            },
            context=ToolExecutionContext(permission=PermissionContext(), workspace_root=tmp_path),
        )
    )

    assert alias_result.is_error
    assert "Missing file_path argument" in alias_result.content
    assert not canonical_result.is_error
    assert target.read_text(encoding="utf-8") == "<html>ok</html>"


def test_websocket_can_load_approval_file_diff_on_demand(monkeypatch) -> None:
    _install_llm_factory(monkeypatch)
    large_patch = "@@ -1 +1,12000 @@\n" + "\n".join(f"+line {index}" for index in range(12000))

    async def _fake_large_approval_loop(*args, **kwargs) -> AsyncIterator[AgentEvent]:
        await _admit_fake_turn(kwargs)
        yield AgentEvent.approval_request(
            tool_call_id="tool_confirm_large",
            tool_name="apply_patch",
            args={"file_path": "demo.txt"},
            diff={
                "format": "structured",
                "stats": {
                    "files_count": 1,
                    "additions": 12000,
                    "deletions": 0,
                },
                "files": [
                    {
                        "path": "demo.txt",
                        "status": "modified",
                        "additions": 12000,
                        "deletions": 0,
                        "patch": large_patch,
                    }
                ],
            },
        )
        yield AgentEvent.done(input_tokens=1, output_tokens=1)

    monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", _fake_large_approval_loop)

    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws?session_id=session_test_approval_file_diff"
        ) as ws:
            _assert_startup_events(ws)

            ws.send_json({"type": "user_message", "content": "trigger large approval"})
            approval = _receive_next_non_task_update(ws)

            assert approval["type"] == "control_request"
            assert approval["request"]["diff"]["files"][0]["path"] == "demo.txt"
            assert approval["request"]["diff"]["files"][0]["patch"] is None
            assert approval["request"]["diff"]["files"][0]["is_large"] is True

            ws.send_json(
                {
                    "type": "approval.file_diff",
                    "tool_call_id": "tool_confirm_large",
                    "path": "demo.txt",
                    "conversation_id": approval["conversation_id"],
                    "turn_id": approval["turn_id"],
                    "message_id": approval["message_id"],
                }
            )
            file_diff = _receive_next_type(ws, "approval.file_diff")

    assert file_diff["type"] == "approval.file_diff"
    assert file_diff["tool_call_id"] == "tool_confirm_large"
    assert file_diff["path"] == "demo.txt"
    assert file_diff["patch"] == large_patch


def test_new_websocket_session_lists_history_without_auto_restoring_active_conversation(monkeypatch, tmp_path) -> None:
    conversation_dir = tmp_path / "conversations"
    repo = ConversationRepository(conversation_dir)
    existing = repo.create_conversation()
    repo.append_transcript_message(
        existing.id,
        {
            "id": "user_seed",
            "role": "user",
            "content": "old conversation",
            "timestamp": "2026-04-18T00:00:00+00:00",
        },
    )

    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", conversation_dir, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", conversation_dir, raising=False)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_fresh_conversation") as ws:
            _assert_startup_events(ws)

            ws.send_json({"type": "conversation.list"})
            listing = _receive_next_non_task_update(ws)

    assert [item["id"] for item in listing["conversations"]] == [existing.id]
    assert listing["active_conversation_id"] is None
    assert listing["active_conversation"] is None
    assert listing["session"]["active_conversation_id"] is None


def test_uploaded_attachment_can_be_opened_after_websocket_reconnect(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.attachments.store.ATTACHMENT_DATA_DIR", tmp_path / "attachments")

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_attachment_reopen") as ws:
            _assert_startup_events(ws)

            response = client.post(
                "/api/uploads",
                params={"session_id": "session_test_attachment_reopen"},
                files={"file": ("note.txt", b"hello persisted attachment", "text/plain")},
            )
            assert response.status_code == 200
            payload = response.json()
            artifact_id = payload["artifact_id"]

        with client.websocket_connect("/ws?session_id=session_test_attachment_reopen") as ws:
            _assert_startup_events(ws)
            ws.send_json({"type": "read_artifact", "artifact_id": artifact_id})
            event = _receive_next_non_task_update(ws)

    assert event["type"] == "artifact_content"
    assert event["artifact_id"] == artifact_id
    assert "hello persisted attachment" in event["content"]


def test_websocket_session_handoff_preserves_active_session(monkeypatch) -> None:
    _install_llm_factory(monkeypatch)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_handoff") as ws_primary:
            _assert_startup_events(ws_primary)

            with client.websocket_connect("/ws?session_id=session_test_handoff") as ws_reconnect:
                _assert_startup_events(ws_reconnect)

                # Simulate stale socket shutdown after a successful reconnect handoff.
                ws_primary.close()

                ws_reconnect.send_json({"type": "ping"})
                pong = _receive_next_non_task_update(ws_reconnect)
                assert pong["type"] == "pong"
                _assert_event_envelope(pong)

                health = client.get("/health")
                assert health.status_code == 200
                assert health.json()["active_sessions"] == 1


def test_read_artifact_tool_can_fall_back_to_persisted_attachments(tmp_path) -> None:
    attachment_store = AttachmentStore(tmp_path / "attachments")
    attachment_store.save(
        artifact_id="art_uploaded_note",
        content="persisted attachment body",
        metadata={"conversation_id": "conv_test"},
    )
    tool = ReadArtifactTool(ArtifactStore(), attachment_store=attachment_store)

    result = asyncio.run(
        tool.execute(
            {"artifact_id": "art_uploaded_note"},
            context=ToolExecutionContext(
                permission=PermissionContext(),
                conversation_id="conv_test",
            ),
        )
    )

    assert result.is_error is False
    assert "persisted attachment body" in result.content
    assert result.content_preview == "persisted attachment body"
    assert result.display_summary == "Read artifact art_uploaded_note"


def test_effective_user_message_includes_structured_attachment_reference() -> None:
    message = _build_effective_user_message(
        "总结这个附件",
        [
            {
                "file_name": "paper.pdf",
                "kind": "document",
                "doc_id": "doc_123",
                "artifact_id": "art_123",
                "indexed_chunks": 8,
            }
        ],
    )

    assert 'artifact_id="art_123"' in message
    assert 'doc_id="doc_123"' in message
    assert 'file_name="paper.pdf"' in message


def test_websocket_user_message_emits_stream_and_done(monkeypatch) -> None:
    _install_llm_factory(monkeypatch)
    monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", _fake_agent_loop)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_chat") as ws:
            _assert_startup_events(ws)

            ws.send_json({"type": "user_message", "content": "hello"})

            third = _receive_next_type(ws, "item.completed")
            fourth = _receive_next_type(ws, "done")

    assert third["type"] == "item.completed"
    assert third["item"]["text"] == "stub reply"
    assert third["conversation_id"].startswith("conv_")
    assert fourth["type"] == "done"
    assert fourth["conversation_id"] == third["conversation_id"]
    assert fourth["usage"]["input_tokens"] == 3
    assert fourth["usage"]["output_tokens"] == 2


def test_websocket_queues_followup_while_run_is_active(monkeypatch) -> None:
    _install_llm_factory(monkeypatch)
    monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", _slow_agent_loop)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_busy_error_code") as ws:
            _assert_startup_events(ws)

            ws.send_json({"type": "user_message", "content": "first"})
            first = _receive_next_type(ws, "item.completed")
            ws.send_json({"type": "user_message", "content": "second"})

            queued = _receive_next_type(ws, "user_message.queue.updated")

    assert first["type"] == "item.completed"
    assert queued["status"] == "queued"
    assert queued["position"] == 1
    assert queued["conversation_id"] == first["conversation_id"]


def test_websocket_steer_promotes_followup_into_active_turn(monkeypatch) -> None:
    _install_llm_factory(monkeypatch)
    monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", _steerable_agent_loop)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_turn_steer") as ws:
            _assert_startup_events(ws)

            ws.send_json({
                "type": "user_message",
                "content": "first",
                "assistant_message_id": "assistant-current",
                "user_message_id": "user-current",
            })
            started = _receive_next_type(ws, "agent.item")
            conversation_id = str(started["conversation_id"])

            ws.send_json({
                "type": "user_message",
                "content": "second",
                "conversation_id": conversation_id,
                "assistant_message_id": "assistant-steer",
                "user_message_id": "user-steer",
            })
            queued = _receive_next_type(ws, "user_message.queue.updated")
            assert queued["status"] == "queued"

            ws.send_json({
                "type": "user_message.queue.steer",
                "conversation_id": conversation_id,
                "message_id": "assistant-steer",
            })
            promoted = _receive_next_type(ws, "user_message.queue.updated")
            steered_chunk = _receive_next_type(ws, "item.completed")
            done = _receive_next_type(ws, "done")

            ws.send_json({"type": "session.sync"})
            synced = _receive_next_type(ws, "session.synced")

    assert promoted["status"] == "dequeued"
    assert promoted["turn_mode"] == "steer"
    assert promoted["target_message_id"] == "assistant-current"
    assert steered_chunk["item"]["text"] == "steered:second"
    assert done["status"] == "completed"
    assert synced["session"]["pending_turn_inputs"] == []


def test_websocket_can_update_session_model(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.4")
    monkeypatch.setenv("OPENAI_AVAILABLE_MODELS", "gpt-5.4,gpt-5.4-mini")
    _install_llm_factory(monkeypatch, _fake_llm_factory)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_model_switch") as ws:
            startup_events = _assert_startup_events(ws)
            initial = next(event for event in startup_events if event["type"] == "llm.model.updated")
            assert initial["type"] == "llm.model.updated"
            assert initial["model"] == "gpt-5.4"

            ws.send_json({"type": "llm.model.set", "model": "gpt-5.4-mini"})
            updated = _receive_next_non_task_update(ws)

    assert updated["type"] == "llm.model.updated"
    assert updated["model"] == "gpt-5.4-mini"


def test_websocket_rejects_unknown_session_model(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.4")
    monkeypatch.setenv("OPENAI_AVAILABLE_MODELS", "gpt-5.4,gpt-5.4-mini")
    _install_llm_factory(monkeypatch, _fake_llm_factory)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_model_switch_invalid") as ws:
            startup_events = _assert_startup_events(ws)
            initial = next(event for event in startup_events if event["type"] == "llm.model.updated")
            assert initial["model"] == "gpt-5.4"

            ws.send_json({"type": "llm.model.set", "model": "deepseek-v4"})
            error = _receive_next_non_task_update(ws)
            updated = _receive_next_non_task_update(ws)

    assert error["type"] == "command.result"
    assert error["level"] == "error"
    assert error["data"]["provider_error_type"] == "model"
    assert updated["type"] == "llm.model.updated"
    assert updated["model"] == "gpt-5.4"
    assert "deepseek-v4" not in updated["available_models"]
