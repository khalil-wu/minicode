from __future__ import annotations

from typing import Any, TYPE_CHECKING, cast

from backend.agent.message import AgentEvent
from backend.ws.events import ServerEventType, is_server_event
from backend.ws.command_results import emit_command_error

if TYPE_CHECKING:
    from backend.ws.handler import WebSocketSession


def _active_conversation_id(session: "WebSocketSession") -> str:
    return str(getattr(session, "active_conversation_id", "") or "").strip()


def _scope_event(event: AgentEvent, conversation_id: str) -> AgentEvent:
    event.data["conversation_id"] = conversation_id
    return event


async def handle_preview_detect(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.preview import detect_dev_servers
    from backend.services.preview_service import (
        no_preview_servers_notice,
        preview_server_detected_event,
        preview_servers_updated_event,
    )

    conversation_id = _active_conversation_id(session)
    if not conversation_id:
        await emit_command_error(session, "preview.detect", "Select a conversation before inspecting previews")
        return True
    try:
        servers = await detect_dev_servers()
    except Exception as exc:
        await emit_command_error(session, "preview.detect", f"Failed to detect dev servers: {exc}")
        return True
    await session._send_event(_scope_event(preview_servers_updated_event(servers), conversation_id))
    for server in servers:
        await session._send_event(_scope_event(preview_server_detected_event(server), conversation_id))
    if not servers:
        await session._send_event(_scope_event(no_preview_servers_notice(), conversation_id))
    return True


async def handle_preview_launch_config(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.preview import load_preview_launch_configs, running_preview_processes
    from backend.services.preview_service import preview_launch_config_event

    conversation_id = _active_conversation_id(session)
    if not conversation_id:
        await emit_command_error(session, "preview.launch.config", "Select a conversation before inspecting previews")
        return True
    try:
        workspace_root = session._resolve_requested_workspace(
            str(data.get("workspace_root") or "").strip() or None
        )
    except ValueError as exc:
        await emit_command_error(session, "preview.launch.config", exc)
        return True
    configs = load_preview_launch_configs(workspace_root)
    await session._send_event(
        _scope_event(preview_launch_config_event(
            workspace_root=workspace_root,
            configs=configs,
            running=running_preview_processes(
                session_id=session.session_id,
                conversation_id=conversation_id,
                workspace_root=workspace_root,
            ),
        ), conversation_id)
    )
    return True


async def handle_preview_launch_start(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.preview import mark_preview_ready, start_preview_launch
    from backend.preview.verifier import wait_until_ready
    from backend.services.preview_service import (
        preview_launch_detected_event,
        preview_launch_started_event,
        preview_verified_event,
    )

    conversation_id = _active_conversation_id(session)
    if not conversation_id:
        await emit_command_error(session, "preview.launch.start", "Select a conversation before starting a preview")
        return True
    try:
        workspace_root = session._resolve_requested_workspace(
            str(data.get("workspace_root") or "").strip() or None
        )
    except ValueError as exc:
        await emit_command_error(session, "preview.launch.start", exc)
        return True
    name = str(data.get("name") or "").strip() or None

    async def broadcast(event: dict[str, Any]) -> None:
        event_type = str(event.get("type", ""))
        if not is_server_event(event_type):
            return
        await session._send_event(
            _scope_event(AgentEvent(
                type=cast(ServerEventType, event_type),
                data={k: v for k, v in event.items() if k != "type"},
            ), conversation_id)
        )

    try:
        process = await start_preview_launch(
            workspace_root,
            name,
            broadcast=broadcast,
            session_id=session.session_id,
            conversation_id=conversation_id,
        )
    except Exception as exc:
        await emit_command_error(session, "preview.launch.start", f"Failed to start preview: {exc}")
        return True
    await session._send_event(_scope_event(preview_launch_started_event(process), conversation_id))
    await session._send_event(_scope_event(preview_launch_detected_event(process), conversation_id))
    verification = await wait_until_ready(process.effective_url, timeout=20.0, interval=1.0)
    if verification.ok:
        await mark_preview_ready(process)
    await session._send_event(_scope_event(preview_verified_event(verification), conversation_id))
    return True


async def handle_preview_launch_stop(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.preview import stop_preview_launch
    from backend.services.preview_service import no_preview_launch_notice, preview_launch_stopped_event

    name = str(data.get("name") or "").strip() or None
    conversation_id = _active_conversation_id(session)
    if not conversation_id:
        await emit_command_error(session, "preview.launch.stop", "Select a conversation before stopping a preview")
        return True
    stopped = await stop_preview_launch(
        name,
        session_id=session.session_id,
        conversation_id=conversation_id,
    )
    for process in stopped:
        await session._send_event(_scope_event(preview_launch_stopped_event(process), conversation_id))
    if not stopped:
        await session._send_event(_scope_event(no_preview_launch_notice(), conversation_id))
    return True


async def handle_preview_navigate(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.preview import verify_preview_url
    from backend.services.preview_service import preview_navigated_event, preview_verified_event, validate_preview_url

    conversation_id = _active_conversation_id(session)
    if not conversation_id:
        await emit_command_error(session, "preview.navigate", "Select a conversation before navigating a preview")
        return True
    url, error_event = validate_preview_url(data, command="preview.navigate")
    if error_event is not None:
        await emit_command_error(session, "preview.navigate", error_event)
        return True
    await session._send_event(_scope_event(preview_navigated_event(url), conversation_id))
    result = await verify_preview_url(url)
    await session._send_event(_scope_event(preview_verified_event(result), conversation_id))
    return True


async def handle_preview_refresh(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.preview_service import preview_refreshed_event

    conversation_id = _active_conversation_id(session)
    if not conversation_id:
        await emit_command_error(session, "preview.refresh", "Select a conversation before refreshing a preview")
        return True
    url = str(data.get("url", "")).strip()
    await session._send_event(_scope_event(preview_refreshed_event(url), conversation_id))
    return True


async def handle_preview_verify(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.preview import verify_preview_url
    from backend.services.preview_service import preview_verified_event, validate_preview_url

    conversation_id = _active_conversation_id(session)
    if not conversation_id:
        await emit_command_error(session, "preview.verify", "Select a conversation before verifying a preview")
        return True
    url, error_event = validate_preview_url(data, command="preview.verify")
    if error_event is not None:
        await emit_command_error(session, "preview.verify", error_event)
        return True
    result = await verify_preview_url(url)
    await session._send_event(_scope_event(preview_verified_event(result), conversation_id))
    return True


HANDLERS: dict[str, Any] = {
    "preview.detect": handle_preview_detect,
    "preview.navigate": handle_preview_navigate,
    "preview.refresh": handle_preview_refresh,
    "preview.launch.config": handle_preview_launch_config,
    "preview.launch.start": handle_preview_launch_start,
    "preview.launch.stop": handle_preview_launch_stop,
    "preview.verify": handle_preview_verify,
}
