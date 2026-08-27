from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from backend.agent.message import AgentEvent
from backend.permissions.network import assess_network_url


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


def validate_preview_url(
    data: dict[str, Any],
    *,
    command: str,
    session_id: str = "",
    conversation_id: str = "",
    workspace_root: str = "",
) -> tuple[str, AgentEvent | None]:
    url = str(data.get("url", "")).strip()
    if not url:
        return "", AgentEvent.error(f"{command} requires a url", recoverable=True)
    try:
        parsed = urlparse(url)
    except ValueError:
        parsed = None
    if parsed is None or parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return "", AgentEvent.error(f"{command} only supports http(s) URLs", recoverable=True)
    if parsed.username or parsed.password:
        return "", AgentEvent.error(
            f"{command} does not allow embedded URL credentials",
            recoverable=True,
            error_type="network_policy",
        )
    assessment = assess_network_url(url)
    if not assessment.allowed:
        from backend.preview.launcher import preview_url_is_owned

        if not preview_url_is_owned(
            url,
            session_id=session_id,
            conversation_id=conversation_id,
            workspace_root=workspace_root,
        ):
            return "", AgentEvent.error(
                "Preview access to a local, private, or unresolved network target "
                "is allowed only for a preview owned by the active conversation. "
                f"{assessment.reason}",
                recoverable=True,
                error_type="network_policy",
            )
    return url, None


def preview_navigated_event(url: str) -> AgentEvent:
    return AgentEvent(type="preview.navigated", data={"url": url})


def preview_refreshed_event(url: str) -> AgentEvent:
    return AgentEvent(type="preview.refreshed", data={"url": url} if url else {})


def preview_verified_event(result: Any) -> AgentEvent:
    return AgentEvent(type="preview.verified", data=result.to_dict())
