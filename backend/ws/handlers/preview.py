from __future__ import annotations

from typing import Any, TYPE_CHECKING, cast

from backend.agent.message import AgentEvent
from backend.ws.command_scope import CommandScope, resolve_command_scope
from backend.ws.events import ServerEventType, is_server_event
from backend.ws.command_results import emit_command_error

if TYPE_CHECKING:
    from backend.ws.handler import WebSocketSession


def _scope_event(event: AgentEvent, scope: CommandScope) -> AgentEvent:
    scope.apply(event.data)
    return event


async def _resolve_scope(
    session: "WebSocketSession",
    data: dict[str, Any],
    command: str,
) -> CommandScope | None:
    try:
        return resolve_command_scope(session, data)
    except ValueError as exc:
        await emit_command_error(session, command, exc)
        return None


async def handle_preview_detect(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.preview import detect_dev_servers
    from backend.services.preview_service import (
        no_preview_servers_notice,
        preview_server_detected_event,
        preview_servers_updated_event,
    )

    scope = await _resolve_scope(session, data, "preview.detect")
    if scope is None:
        return True
    try:
        servers = await detect_dev_servers()
    except Exception as exc:
        await emit_command_error(session, "preview.detect", f"Failed to detect dev servers: {exc}")
        return True
    await session.send_event(_scope_event(preview_servers_updated_event(servers), scope))
    for server in servers:
        await session.send_event(_scope_event(preview_server_detected_event(server), scope))
    if not servers:
        await session.send_event(_scope_event(no_preview_servers_notice(), scope))
    return True


async def handle_preview_launch_config(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.preview import PreviewLaunchConfigError, load_preview_launch_configs, running_preview_processes
    from backend.services.preview_service import preview_launch_config_event

    scope = await _resolve_scope(session, data, "preview.launch.config")
    if scope is None:
        return True
    workspace_root = scope.workspace_root
    try:
        configs = load_preview_launch_configs(workspace_root)
    except PreviewLaunchConfigError as exc:
        # An empty config list means "no preview configured"; a rejected
        # launch.json must not be reported that way.
        await emit_command_error(
            session,
            "preview.launch.config",
            str(exc),
            data={"workspace_root": workspace_root, "source": exc.source, "reason": exc.reason},
        )
        return True
    await session.send_event(
        _scope_event(preview_launch_config_event(
            workspace_root=workspace_root,
            configs=configs,
            running=running_preview_processes(
                session_id=session.session_id,
                conversation_id=scope.conversation_id,
                workspace_root=workspace_root,
            ),
        ), scope)
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

    scope = await _resolve_scope(session, data, "preview.launch.start")
    if scope is None:
        return True
    workspace_root = scope.workspace_root
    name = str(data.get("name") or "").strip() or None

    async def broadcast(event: dict[str, Any]) -> None:
        event_type = str(event.get("type", ""))
        if not is_server_event(event_type):
            return
        await session.send_event(
            _scope_event(AgentEvent(
                type=cast(ServerEventType, event_type),
                data={k: v for k, v in event.items() if k != "type"},
            ), scope)
        )

    try:
        process = await start_preview_launch(
            workspace_root,
            name,
            broadcast=broadcast,
            session_id=session.session_id,
            conversation_id=scope.conversation_id,
        )
    except Exception as exc:
        await emit_command_error(session, "preview.launch.start", f"Failed to start preview: {exc}")
        return True
    await session.send_event(_scope_event(preview_launch_started_event(process), scope))
    await session.send_event(_scope_event(preview_launch_detected_event(process), scope))
    verification = await wait_until_ready(process.effective_url, timeout=20.0, interval=1.0)
    if verification.ok:
        await mark_preview_ready(process)
    await session.send_event(_scope_event(preview_verified_event(verification), scope))
    return True


async def handle_preview_launch_stop(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.preview import stop_preview_launch
    from backend.services.preview_service import no_preview_launch_notice, preview_launch_stopped_event

    name = str(data.get("name") or "").strip() or None
    scope = await _resolve_scope(session, data, "preview.launch.stop")
    if scope is None:
        return True
    stopped = []
    try:
        stopped = await stop_preview_launch(
            name,
            session_id=session.session_id,
            conversation_id=scope.conversation_id,
        )
    except RuntimeError as exc:
        # A preview whose exit could not be proven keeps running; say so instead
        # of reporting a stop that did not happen.
        await emit_command_error(session, "preview.launch.stop", str(exc))
        return True
    for process in stopped:
        await session.send_event(_scope_event(preview_launch_stopped_event(process), scope))
    if not stopped:
        await session.send_event(_scope_event(no_preview_launch_notice(), scope))
    return True


async def handle_preview_navigate(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.preview import verify_preview_url
    from backend.services.preview_service import preview_navigated_event, preview_verified_event, validate_preview_url

    scope = await _resolve_scope(session, data, "preview.navigate")
    if scope is None:
        return True
    url, error_event = validate_preview_url(
        data,
        command="preview.navigate",
        session_id=session.session_id,
        conversation_id=scope.conversation_id,
        workspace_root=scope.workspace_root,
    )
    if error_event is not None:
        await emit_command_error(session, "preview.navigate", error_event)
        return True
    await session.send_event(_scope_event(preview_navigated_event(url), scope))
    result = await verify_preview_url(url)
    await session.send_event(_scope_event(preview_verified_event(result), scope))
    return True


async def handle_preview_refresh(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.preview_service import preview_refreshed_event, validate_preview_url

    scope = await _resolve_scope(session, data, "preview.refresh")
    if scope is None:
        return True
    url = str(data.get("url", "")).strip()
    if url:
        url, error_event = validate_preview_url(
            {"url": url},
            command="preview.refresh",
            session_id=session.session_id,
            conversation_id=scope.conversation_id,
            workspace_root=scope.workspace_root,
        )
        if error_event is not None:
            await emit_command_error(session, "preview.refresh", error_event)
            return True
    await session.send_event(_scope_event(preview_refreshed_event(url), scope))
    return True


async def handle_preview_verify(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.preview import verify_preview_url
    from backend.services.preview_service import preview_verified_event, validate_preview_url

    scope = await _resolve_scope(session, data, "preview.verify")
    if scope is None:
        return True
    url, error_event = validate_preview_url(
        data,
        command="preview.verify",
        session_id=session.session_id,
        conversation_id=scope.conversation_id,
        workspace_root=scope.workspace_root,
    )
    if error_event is not None:
        await emit_command_error(session, "preview.verify", error_event)
        return True
    result = await verify_preview_url(url)
    await session.send_event(_scope_event(preview_verified_event(result), scope))
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
