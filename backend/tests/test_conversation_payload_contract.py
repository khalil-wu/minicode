from __future__ import annotations

import asyncio
from types import SimpleNamespace
from pathlib import Path

from backend.services.conversation_payload_service import (
    parse_conversation_create_request,
)
from backend.ws.handlers.conversation import handle_conversation_create
from backend.ws.session_lifecycle import SessionLifecycle
from backend.agent.message import AgentEvent


def test_conversation_create_permission_mode_defaults_only_when_absent_or_blank() -> None:
    for payload in ({}, {"permission_mode": ""}, {"permission_mode": "  "}):
        request = parse_conversation_create_request(payload)
        assert request.permission_mode == "confirm"
        assert request.error_event is None


def test_conversation_create_can_persist_a_main_task_without_activating_it() -> None:
    assert parse_conversation_create_request({"conversation_type":"main", "activate":False}).activate is False
    assert parse_conversation_create_request({"conversation_type":"main"}).activate is True
    assert parse_conversation_create_request({"conversation_type":"side_chat", "activate":True}).activate is False


def test_conversation_create_rejects_invalid_explicit_permission_mode() -> None:
    request = parse_conversation_create_request({"permission_mode": "unattended"})

    assert request.permission_mode == "confirm"
    assert request.error_event is not None
    assert request.error_event.data["error_code"] == "invalid_permission_mode"


def test_conversation_create_handler_does_not_create_on_invalid_permission_mode() -> None:
    events = []

    async def send_event(event) -> None:
        events.append(event)

    session = SimpleNamespace(send_event=send_event)
    created = asyncio.run(handle_conversation_create(
        session,
        {"permission_mode": "unattended"},
    ))

    assert created is True
    assert len(events) == 1
    assert events[0].type == "command.result"
    assert events[0].data["level"] == "error"
    assert events[0].data["data"]["error_code"] == "invalid_permission_mode"


def test_conversation_create_rejects_permission_mode_disallowed_by_managed_requirements() -> None:
    from backend.config import AppConfig, LLMSettings
    from backend.config_layers import ConfigLayerStack
    from backend.config_requirements import RequirementSource, RequirementsLayerEntry, compose_requirements

    requirements = compose_requirements([RequirementsLayerEntry(
        RequirementSource("system_requirements_toml", location="requirements.toml"),
        {"allowed_approval_policies": ["on-request"], "allowed_sandbox_modes": ["read-only", "workspace-write"]},
    )])
    events = []

    async def send_event(event) -> None:
        events.append(event)

    session = SimpleNamespace(config=AppConfig(llm=LLMSettings(api_key=""), config_layer_stack=ConfigLayerStack(layers=(), requirements=requirements)),
                              send_event=send_event)
    assert asyncio.run(handle_conversation_create(session, {"permission_mode": "bypass"})) is True
    assert len(events) == 1
    assert events[0].type == "command.result"
    assert events[0].data["level"] == "error"
    assert "Managed requirement" in events[0].data["message"]


def test_isolated_worktree_failure_does_not_leave_a_shared_success_record(
    monkeypatch,
    tmp_path: Path,
) -> None:
    conversation = SimpleNamespace(
        id="conv-isolation-failure",
        workspace_root=str(tmp_path),
        execution_cwd=str(tmp_path),
        worktree_path="",
        git_isolated=True,
    )
    deleted: list[str] = []
    events = []

    class Repository:
        def delete_conversation(self, conversation_id: str) -> bool:
            deleted.append(conversation_id)
            return True

    async def send_event(event) -> None:
        events.append(event)

    session = SimpleNamespace(
        conversation_repo=Repository(),
        main_worktree_root=lambda root: root,
        active_conversation_id=conversation.id,
        active_conversation=conversation,
        send_event=send_event,
    )
    lifecycle = SessionLifecycle(session)
    from backend.config import load_config_layer_stack
    from backend.permissions.context import PermissionContext

    session.permission_context_for_conversation = lambda _conversation, **_kwargs: PermissionContext(workspace_root=tmp_path)
    session.config = SimpleNamespace(config_layer_stack=load_config_layer_stack(cwd=tmp_path))
    monkeypatch.setattr(
        "backend.services.conversation_payload_service.create_isolated_worktree_binding",
        lambda *_args, **_kwargs: SimpleNamespace(
            created=False,
            error_event=AgentEvent.error("worktree unavailable"),
            conversation_id=conversation.id,
        ),
    )

    result = asyncio.run(lifecycle.create_isolated_conversation_worktree(conversation))

    assert result is None
    assert deleted == [conversation.id]
    assert len(events) == 1
    assert events[0].type == "command.result"
    assert events[0].data["level"] == "error"
