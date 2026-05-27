from __future__ import annotations

from typing import Any, TYPE_CHECKING, cast

from backend.agent.message import AgentEvent
from backend.ws.events import ServerEventType, is_server_event

if TYPE_CHECKING:
    from backend.ws.handler import WebSocketSession


async def handle_preview_detect(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.preview import detect_dev_servers

    try:
        servers = await detect_dev_servers()
    except Exception as exc:
        await session._send_event(
            AgentEvent.error(f"Failed to detect dev servers: {exc}", recoverable=True)
        )
        return True
    payload = [server.to_dict() for server in servers]
    await session._send_event(AgentEvent(type="preview.servers.updated", data={"servers": payload}))
    for server in servers:
        await session._send_event(AgentEvent(type="preview.server.detected", data=server.to_dict()))
    if not servers:
        await session._send_event(
            AgentEvent(type="system_notice", data={"content": "No dev servers detected on common ports"})
        )
    return True


async def handle_preview_launch_config(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.preview import load_preview_launch_configs, running_preview_processes

    try:
        workspace_root = session._resolve_requested_workspace(
            str(data.get("workspace_root") or "").strip() or None
        )
    except ValueError as exc:
        await session._send_event(AgentEvent.error(str(exc), recoverable=True))
        return True
    configs = load_preview_launch_configs(workspace_root)
    running = [process.to_dict() for process in running_preview_processes()]
    await session._send_event(
        AgentEvent(
            type="preview.launch.config",
            data={
                "workspace_root": str(workspace_root),
                "configs": [config.to_dict() for config in configs],
                "running": running,
            },
        )
    )
    return True


async def handle_preview_launch_start(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.preview import start_preview_launch
    from backend.preview.verifier import wait_until_ready

    try:
        workspace_root = session._resolve_requested_workspace(
            str(data.get("workspace_root") or "").strip() or None
        )
    except ValueError as exc:
        await session._send_event(AgentEvent.error(str(exc), recoverable=True))
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
        await session._send_event(AgentEvent.error(f"Failed to start preview: {exc}", recoverable=True))
        return True
    payload = process.to_dict()
    await session._send_event(AgentEvent(type="preview.launch.started", data=payload))
    await session._send_event(
        AgentEvent(
            type="preview.server.detected",
            data={
                "port": process.config.port,
                "url": process.config.url,
                "name": process.config.name,
                "framework": "launch",
            },
        )
    )
    result = await wait_until_ready(process.config.url, timeout=20.0, interval=1.0)
    await session._send_event(AgentEvent(type="preview.verified", data=result.to_dict()))
    return True


async def handle_preview_launch_stop(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.preview import stop_preview_launch

    name = str(data.get("name") or "").strip() or None
    stopped = await stop_preview_launch(name)
    for process in stopped:
        await session._send_event(AgentEvent(type="preview.launch.stopped", data=process.to_dict()))
    if not stopped:
        await session._send_event(
            AgentEvent(type="system_notice", data={"content": "No preview launch process is running"})
        )
    return True


async def handle_preview_navigate(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.preview import verify_preview_url

    url = str(data.get("url", "")).strip()
    if not url:
        await session._send_event(AgentEvent.error("preview.navigate requires a url", recoverable=True))
        return True
    if not (url.startswith("http://") or url.startswith("https://")):
        await session._send_event(
            AgentEvent.error("preview.navigate only supports http(s) URLs", recoverable=True)
        )
        return True
    await session._send_event(AgentEvent(type="preview.navigated", data={"url": url}))
    result = await verify_preview_url(url)
    await session._send_event(AgentEvent(type="preview.verified", data=result.to_dict()))
    return True


async def handle_preview_refresh(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    url = str(data.get("url", "")).strip()
    payload = {"url": url} if url else {}
    await session._send_event(AgentEvent(type="preview.refreshed", data=payload))
    return True


async def handle_preview_verify(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.preview import verify_preview_url

    url = str(data.get("url", "")).strip()
    if not url:
        await session._send_event(AgentEvent.error("preview.verify requires a url", recoverable=True))
        return True
    result = await verify_preview_url(url)
    await session._send_event(AgentEvent(type="preview.verified", data=result.to_dict()))
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
