from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.commands.catalog import get_builtin_command_names
from backend.config import PermissionSettings
from backend.artifact.store import ArtifactStore
from backend.permissions.checker import PermissionChecker
from backend.tools.command_tool import RunCommandTool
from backend.tools.read_file import ReadFileTool
from backend.tools.registry import ToolRegistry
from backend.tools.base import PermissionLevel
from backend.ws.handler import WebSocketSession
from backend.ws.command_dispatcher import SessionCommandDispatcher
from backend.ws.turn_wait_state import TurnWaitState
from backend.ws.handlers.conversation import (
    handle_conversation_permission_mode_set,
    handle_conversation_permission_rules_list,
    handle_conversation_permission_rules_add,
    handle_conversation_permission_rules_remove,
)


class _FakeConversationRepo:
    def __init__(self) -> None:
        self.record = SimpleNamespace(
            id="conv-1",
            revision=1,
            permission_mode="plan",
            permission_deny_rules=["run_existing"],
            permission_overrides={"write_file": "confirm"},
        )
        self.other_record = SimpleNamespace(
            id="conv-2",
            revision=1,
            permission_mode="confirm",
            permission_deny_rules=[],
            permission_overrides={},
        )
        self.records = {
            self.record.id: self.record,
            self.other_record.id: self.other_record,
        }

    def get_conversation(self, conversation_id: str):
        return self.records.get(conversation_id)

    def update_permission_rules(
        self,
        conversation_id: str,
        *,
        deny_rules: list[str],
        overrides: dict[str, str],
    ):
        if conversation_id != self.record.id:
            return None
        self.record.permission_deny_rules = list(deny_rules)
        self.record.permission_overrides = dict(overrides)
        self.record.revision += 1
        return self.record

    def update_permission_mode(self, conversation_id: str, mode: str):
        record = self.records.get(conversation_id)
        if record is None:
            return None
        record.permission_mode = mode
        record.revision += 1
        return record


def _make_session():
    session = WebSocketSession.__new__(WebSocketSession)
    session.session_id = "session-1"
    session.turn_wait_state = TurnWaitState()
    session.event_outbox = SimpleNamespace(connected=True)
    session.conversation_repo = _FakeConversationRepo()
    session.conversation_runtime = SimpleNamespace(
        active_conversation_id="conv-1",
        active_conversation=session.conversation_repo.record,
    )
    session.permission_checker = SimpleNamespace(policy_snapshot=lambda: {"always_deny": ["mcp__*"]})
    session.permission_context = SimpleNamespace(
        mode="plan",
        source="conversation.runtime",
        tool_deny_rules=["run_existing"],
        session_overrides={"write_file": PermissionLevel.CONFIRM},
    )
    session.session_lifecycle = SimpleNamespace()
    session.ws_manager = None
    session.command_dispatcher = object.__new__(SessionCommandDispatcher)
    session.command_dispatcher._session = session

    command_results: list[dict[str, object]] = []
    rule_updates: list[dict[str, object]] = []
    runtime_updates: list[bool] = []
    context_updates: list[dict[str, object]] = []
    sent_events: list[dict[str, object]] = []
    mode_updates: list[bool] = []
    auto_approved: list[dict[str, object]] = []
    conversation_lists: list[bool] = []

    async def emit_command_result(command: str, message: str, **kwargs):
        command_results.append({"command": command, "message": message, **kwargs})

    async def emit_permission_rules_updated(**kwargs):
        rule_updates.append(dict(kwargs))

    async def send_task_runtime_update():
        runtime_updates.append(True)

    def set_permission_context_rules(*, session_overrides, tool_deny_rules, source):
        context_updates.append(
            {
                "session_overrides": dict(session_overrides),
                "tool_deny_rules": list(tool_deny_rules),
                "source": source,
            }
        )
        session.permission_context = SimpleNamespace(
            mode=session.permission_context.mode,
            source=source,
            tool_deny_rules=list(tool_deny_rules),
            session_overrides=dict(session_overrides),
        )

    def set_permission_context_mode(mode: str, *, source: str):
        session.permission_context = SimpleNamespace(
            mode=mode,
            source=source,
            tool_deny_rules=list(session.permission_context.tool_deny_rules),
            session_overrides=dict(session.permission_context.session_overrides),
        )
        return True

    async def emit_permission_mode_updated():
        mode_updates.append(True)

    async def auto_approve_pending_tool_approvals(**kwargs):
        auto_approved.append(dict(kwargs))
        return ["tool-approval-1"]

    async def send_conversation_list():
        conversation_lists.append(True)

    async def send_event(event):
        sent_events.append(event.to_ws_message())

    session.emit_command_result = emit_command_result
    session.emit_permission_rules_updated = emit_permission_rules_updated
    session.emit_permission_mode_updated = emit_permission_mode_updated
    session.session_lifecycle.send_task_runtime_update = send_task_runtime_update
    session.set_permission_context_mode = set_permission_context_mode
    session.set_permission_context_rules = set_permission_context_rules
    session.auto_approve_pending_tool_approvals = auto_approve_pending_tool_approvals
    session.send_conversation_list = send_conversation_list
    session.send_event = send_event

    return session, command_results, rule_updates, runtime_updates, context_updates, sent_events, mode_updates, auto_approved, conversation_lists


def test_builtin_command_catalog_includes_permission_rule_protocol_commands() -> None:
    names = set(get_builtin_command_names())

    assert "conversation.permission.rules.list" in names
    assert "conversation.permission.rules.add" in names
    assert "conversation.permission.rules.remove" in names


def test_permission_mode_bypass_auto_approves_pending_tool_requests() -> None:
    (
        session,
        _command_results,
        _rule_updates,
        runtime_updates,
        _context_updates,
        _sent_events,
        mode_updates,
        auto_approved,
        conversation_lists,
    ) = _make_session()

    asyncio.run(
        handle_conversation_permission_mode_set(
            session,
            {
                "mode": "bypass",
                "source": "composer.footer",
            },
        )
    )

    assert session.conversation_repo.record.permission_mode == "bypass"
    assert session.permission_context.mode == "bypass"
    assert session.permission_context.source == "composer.footer"
    assert mode_updates == [True]
    assert auto_approved == [
        {
            "reason": "permission_mode_bypass",
            "conversation_id": "conv-1",
            "only_auto_allowed": False,
        }
    ]
    assert runtime_updates == [True]
    assert conversation_lists == [True]


def test_permission_mode_auto_approves_only_auto_allowed_pending_tool_requests() -> None:
    (
        session,
        _command_results,
        _rule_updates,
        runtime_updates,
        _context_updates,
        _sent_events,
        mode_updates,
        auto_approved,
        conversation_lists,
    ) = _make_session()

    asyncio.run(
        handle_conversation_permission_mode_set(
            session,
            {
                "mode": "auto",
                "source": "composer.footer",
            },
        )
    )

    assert session.conversation_repo.record.permission_mode == "auto"
    assert session.permission_context.mode == "auto"
    assert mode_updates == [True]
    assert auto_approved == [
        {
            "reason": "permission_mode_auto",
            "conversation_id": "conv-1",
            "only_auto_allowed": True,
        }
    ]
    assert runtime_updates == [True]
    assert conversation_lists == [True]


def test_permission_mode_explicit_conversation_id_updates_non_active_without_runtime_side_effects() -> None:
    (
        session,
        _command_results,
        _rule_updates,
        runtime_updates,
        _context_updates,
        _sent_events,
        mode_updates,
        auto_approved,
        conversation_lists,
    ) = _make_session()

    asyncio.run(
        handle_conversation_permission_mode_set(
            session,
            {
                "mode": "bypass",
                "conversation_id": "conv-2",
                "source": "frontend.ui",
            },
        )
    )

    assert session.conversation_repo.record.permission_mode == "plan"
    assert session.conversation_repo.other_record.permission_mode == "bypass"
    assert session.permission_context.mode == "plan"
    assert mode_updates == []
    assert auto_approved == []
    assert runtime_updates == []
    assert conversation_lists == [True]


def test_user_message_bypass_rechecks_pending_approvals_when_context_already_bypass() -> None:
    session, *_rest = _make_session()
    auto_approved: list[dict[str, object]] = []
    mode_updates: list[bool] = []
    session.permission_context = SimpleNamespace(
        mode="bypass",
        source="user_message",
        tool_deny_rules=[],
        session_overrides={},
    )

    def unchanged_permission_context_mode(mode: str, *, source: str):
        assert mode == "bypass"
        assert source == "user_message"
        return False

    async def emit_permission_mode_updated():
        mode_updates.append(True)

    async def auto_approve_pending_tool_approvals(**kwargs):
        auto_approved.append(dict(kwargs))
        return ["tool-approval-1"]

    session.set_permission_context_mode = unchanged_permission_context_mode
    session.emit_permission_mode_updated = emit_permission_mode_updated
    session.auto_approve_pending_tool_approvals = auto_approve_pending_tool_approvals

    asyncio.run(
        session.command_dispatcher._handle_user_message_permission(
            "bypass", "conv-1"
        )
    )

    assert mode_updates == []
    assert auto_approved == [
        {
            "reason": "permission_mode_bypass",
            "conversation_id": "conv-1",
        }
    ]


def test_auto_approve_pending_tool_approvals_skips_user_questions() -> None:
    session = WebSocketSession.__new__(WebSocketSession)
    session.turn_wait_state = TurnWaitState()
    session.approval_diff_cache = {}
    session.turn_wait_state.pending_approval_payloads = {
        "tool-1": {
            "type": "control_request",
            "request_id": "tool-1",
            "conversation_id": "conv-1",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "mcp__websearch__fetch_page",
                "input": {"url": "https://example.test"},
            },
        },
        "control-tool-1": {
            "type": "control_request",
            "request_id": "control-tool-1",
            "conversation_id": "conv-1",
            "request": {"subtype": "can_use_tool", "tool_name": "run_command"},
        },
        "ask-1": {
            "type": "control_request",
            "request_id": "ask-1",
            "conversation_id": "conv-1",
            "request": {"subtype": "elicitation", "question": "Need input"},
        },
    }
    sent_events: list[dict[str, object]] = []

    async def send_event(event):
        sent_events.append(event.to_ws_message())

    session.send_event = send_event

    async def run_check():
        loop = asyncio.get_running_loop()
        tool_future = loop.create_future()
        control_tool_future = loop.create_future()
        ask_future = loop.create_future()
        session.turn_wait_state.pending_approvals = {
            "tool-1": tool_future,
            "control-tool-1": control_tool_future,
        }
        session.turn_wait_state.pending_elicitations["ask-1"] = ask_future

        approved = await session.auto_approve_pending_tool_approvals(
            reason="permission_mode_bypass",
            conversation_id="conv-1",
        )

        assert approved == ["tool-1", "control-tool-1"]
        assert tool_future.result()["action"] == "approve"
        assert control_tool_future.result()["action"] == "approve"
        assert not ask_future.done()

    asyncio.run(run_check())

    assert sent_events == [
        {
            "type": "approval.cancelled",
            "request_ids": ["tool-1", "control-tool-1"],
            "reason": "permission_mode_bypass",
            "conversation_id": "conv-1",
        }
    ]


def test_auto_approve_pending_tool_approvals_filters_auto_mode_permissions() -> None:
    session = WebSocketSession.__new__(WebSocketSession)
    session.turn_wait_state = TurnWaitState()
    session.approval_diff_cache = {}
    session.permission_checker = PermissionChecker(PermissionSettings())
    session.permission_context = session.permission_checker.build_context(mode="auto", source="test")
    session.tool_registry = ToolRegistry()
    artifacts = ArtifactStore()
    session.tool_registry.register(ReadFileTool(artifacts))
    session.tool_registry.register(RunCommandTool(artifacts))
    session.turn_wait_state.pending_approval_payloads = {
        "read-1": {
            "type": "control_request",
            "request_id": "read-1",
            "conversation_id": "conv-1",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "read_file",
                "input": {"file_path": "README.md"},
            },
        },
        "command-1": {
            "type": "control_request",
            "request_id": "command-1",
            "conversation_id": "conv-1",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "run_command",
                "input": {"command": "npm run build"},
            },
        },
    }
    sent_events: list[dict[str, object]] = []

    async def send_event(event):
        sent_events.append(event.to_ws_message())

    session.send_event = send_event

    async def run_check():
        loop = asyncio.get_running_loop()
        read_future = loop.create_future()
        command_future = loop.create_future()
        session.turn_wait_state.pending_approvals = {
            "read-1": read_future,
            "command-1": command_future,
        }

        approved = await session.auto_approve_pending_tool_approvals(
            reason="permission_mode_auto",
            conversation_id="conv-1",
            only_auto_allowed=True,
        )

        assert approved == ["read-1"]
        assert read_future.result()["action"] == "approve"
        assert not command_future.done()

    asyncio.run(run_check())

    assert sent_events == [
        {
            "type": "approval.cancelled",
            "request_ids": ["read-1"],
            "reason": "permission_mode_auto",
            "conversation_id": "conv-1",
        }
    ]


def test_permission_rule_list_emits_authoritative_command_result() -> None:
    session, command_results, rule_updates, *_ = _make_session()

    asyncio.run(
        handle_conversation_permission_rules_list(
            session,
            {
                "conversation_id": "conv-1",
                "source": "slash:/permissions",
            },
        )
    )

    assert rule_updates == [{"conversation_id": "conv-1", "source": "slash:/permissions"}]
    assert command_results == [
        {
            "command": "permissions.rules.list",
            "message": "Permission rules: mode plan | session deny 1 | overrides 1 | system deny 1",
            "data": {
                "conversation_id": "conv-1",
                "rules": {
                    "mode": "plan",
                    "context_source": "conversation.runtime",
                    "system_deny": [{"pattern": "mcp__*", "source": "system.always_deny"}],
                    "session_deny": [{"pattern": "run_existing", "source": "conversation.runtime"}],
                    "session_overrides": [
                        {
                            "pattern": "write_file",
                            "level": "confirm",
                            "source": "conversation.runtime",
                        }
                    ],
                    "session_prompt_rules": [],
                },
            },
        }
    ]


def test_permission_rule_add_updates_runtime_and_emits_success_result() -> None:
    (
        session,
        command_results,
        rule_updates,
        runtime_updates,
        context_updates,
        *_,
    ) = _make_session()

    asyncio.run(
        handle_conversation_permission_rules_add(
            session,
            {
                "conversation_id": "conv-1",
                "rule_kind": "deny",
                "pattern": "run_*",
                "source": "slash:/permissions",
            },
        )
    )

    assert session.conversation_repo.record.permission_deny_rules == ["run_existing", "run_*"]
    assert context_updates == [
        {
            "session_overrides": {"write_file": PermissionLevel.CONFIRM},
            "tool_deny_rules": ["run_existing", "run_*"],
            "source": "slash:/permissions",
        }
    ]
    assert runtime_updates == [True]
    assert rule_updates == [{"conversation_id": "conv-1", "source": "slash:/permissions"}]
    assert command_results == [
        {
            "command": "permissions.rules.add",
            "message": "Added deny rule: run_*",
            "level": "success",
            "data": {
                "conversation_id": "conv-1",
                "rule_kind": "deny",
                "pattern": "run_*",
                "revision": 2,
                "projection_errors": [],
            },
        }
    ]


def test_permission_rule_remove_updates_runtime_and_emits_success_result() -> None:
    (
        session,
        command_results,
        rule_updates,
        runtime_updates,
        context_updates,
        *_,
    ) = _make_session()

    asyncio.run(
        handle_conversation_permission_rules_remove(
            session,
            {
                "conversation_id": "conv-1",
                "rule_kind": "override",
                "pattern": "write_file",
                "source": "slash:/permissions",
            },
        )
    )

    assert session.conversation_repo.record.permission_overrides == {}
    assert context_updates == [
        {
            "session_overrides": {},
            "tool_deny_rules": ["run_existing"],
            "source": "slash:/permissions",
        }
    ]
    assert runtime_updates == [True]
    assert rule_updates == [{"conversation_id": "conv-1", "source": "slash:/permissions"}]
    assert command_results == [
        {
            "command": "permissions.rules.remove",
            "message": "Removed override rule: write_file",
            "level": "success",
            "data": {
                "conversation_id": "conv-1",
                "rule_kind": "override",
                "pattern": "write_file",
                "revision": 2,
                "projection_errors": [],
            },
        }
    ]
