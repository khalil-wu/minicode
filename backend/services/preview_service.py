from __future__ import annotations

from typing import Any

from backend.agent.message import AgentEvent


def preview_servers_updated_event(servers: list[Any]) -> AgentEvent:
    return AgentEvent(
        type="preview.servers.updated",
        data={"servers": [server.to_dict() for server in servers]},
    )


def preview_server_detected_event(server: Any) -> AgentEvent:
    return AgentEvent(type="preview.server.detected", data=server.to_dict())


def no_preview_servers_notice() -> AgentEvent:
    return AgentEvent(type="system_notice", data={"content": "No dev servers detected on common ports"})


def preview_launch_config_event(
    *,
    workspace_root: Any,
    configs: list[Any],
    running: list[Any],
) -> AgentEvent:
    return AgentEvent(
        type="preview.launch.config",
        data={
            "workspace_root": str(workspace_root),
            "configs": [config.to_dict() for config in configs],
            "running": [process.to_dict() for process in running],
        },
    )


def preview_launch_started_event(process: Any) -> AgentEvent:
    return AgentEvent(type="preview.launch.started", data=process.to_dict())


def preview_launch_detected_event(process: Any) -> AgentEvent:
    return AgentEvent(
        type="preview.server.detected",
        data={
            "port": process.config.port,
            "url": process.config.url,
            "name": process.config.name,
            "framework": "launch",
        },
    )


def preview_launch_stopped_event(process: Any) -> AgentEvent:
    return AgentEvent(type="preview.launch.stopped", data=process.to_dict())


def no_preview_launch_notice() -> AgentEvent:
    return AgentEvent(type="system_notice", data={"content": "No preview launch process is running"})


def validate_preview_url(data: dict[str, Any], *, command: str) -> tuple[str, AgentEvent | None]:
    url = str(data.get("url", "")).strip()
    if not url:
        return "", AgentEvent.error(f"{command} requires a url", recoverable=True)
    if command == "preview.navigate" and not (url.startswith("http://") or url.startswith("https://")):
        return "", AgentEvent.error("preview.navigate only supports http(s) URLs", recoverable=True)
    return url, None


def preview_navigated_event(url: str) -> AgentEvent:
    return AgentEvent(type="preview.navigated", data={"url": url})


def preview_refreshed_event(url: str) -> AgentEvent:
    return AgentEvent(type="preview.refreshed", data={"url": url} if url else {})


def preview_verified_event(result: Any) -> AgentEvent:
    return AgentEvent(type="preview.verified", data=result.to_dict())
