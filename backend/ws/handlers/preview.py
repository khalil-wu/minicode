from __future__ import annotations

from typing import Any, TYPE_CHECKING, cast

from backend.agent.message import AgentEvent
from backend.ws.events import ServerEventType, is_server_event
from backend.ws.command_results import emit_command_error

if TYPE_CHECKING:
    from backend.ws.handler import WebSocketSession


async def handle_preview_detect(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.preview import detect_dev_servers
    from backend.services.preview_service import (
        no_preview_servers_notice,
        preview_server_detected_event,
        preview_servers_updated_event,
    )

    try:
        servers = await detect_dev_servers()
    except Exception as exc:
        await emit_command_error(session, "preview.detect", f"Failed to detect dev servers: {exc}")
        return True
    await session._send_event(preview_servers_updated_event(servers))
    for server in servers:
        await session._send_event(preview_server_detected_event(server))
    if not servers:
        await session._send_event(no_preview_servers_notice())
    return True


async def handle_preview_launch_config(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.preview import load_preview_launch_configs, running_preview_processes
    from backend.services.preview_service import preview_launch_config_event

    try:
        workspace_root = session._resolve_requested_workspace(
            str(data.get("workspace_root") or "").strip() or None
        )
    except ValueError as exc:
        await emit_command_error(session, "preview.launch.config", exc)
        return True
    configs = load_preview_launch_configs(workspace_root)
    await session._send_event(
        preview_launch_config_event(
            workspace_root=workspace_root,
            configs=configs,
            running=running_preview_processes(),
        )
    )
    return True


async def handle_preview_launch_start(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.preview import start_preview_launch
    from backend.preview.verifier import wait_until_ready
    from backend.services.preview_service import (
        preview_launch_detected_event,
        preview_launch_started_event,
        preview_verified_event,
    )

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
            AgentEvent(
                type=cast(ServerEventType, event_type),
                data={k: v for k, v in event.items() if k != "type"},
            )
        )

    try:
        process = await start_preview_launch(workspace_root, name, broadcast=broadcast)
    except Exception as exc:
        await emit_command_error(session, "preview.launch.start", f"Failed to start preview: {exc}")
        return True
    await session._send_event(preview_launch_started_event(process))
    await session._send_event(preview_launch_detected_event(process))
    result = await wait_until_ready(process.config.url, timeout=20.0, interval=1.0)
    await session._send_event(preview_verified_event(result))
    return True


async def handle_preview_launch_stop(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.preview import stop_preview_launch
    from backend.services.preview_service import no_preview_launch_notice, preview_launch_stopped_event

    name = str(data.get("name") or "").strip() or None
    stopped = await stop_preview_launch(name)
    for process in stopped:
        await session._send_event(preview_launch_stopped_event(process))
    if not stopped:
        await session._send_event(no_preview_launch_notice())
    return True


async def handle_preview_navigate(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.preview import verify_preview_url
    from backend.services.preview_service import preview_navigated_event, preview_verified_event, validate_preview_url

    url, error_event = validate_preview_url(data, command="preview.navigate")
    if error_event is not None:
        await emit_command_error(session, "preview.navigate", error_event)
        return True
    await session._send_event(preview_navigated_event(url))
    result = await verify_preview_url(url)
    await session._send_event(preview_verified_event(result))
    return True


async def handle_preview_refresh(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.preview_service import preview_refreshed_event

    url = str(data.get("url", "")).strip()
    await session._send_event(preview_refreshed_event(url))
    return True


async def handle_preview_verify(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.preview import verify_preview_url
    from backend.services.preview_service import preview_verified_event, validate_preview_url

    url, error_event = validate_preview_url(data, command="preview.verify")
    if error_event is not None:
        await emit_command_error(session, "preview.verify", error_event)
        return True
    result = await verify_preview_url(url)
    await session._send_event(preview_verified_event(result))
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
