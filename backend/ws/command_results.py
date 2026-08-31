from __future__ import annotations

from typing import Any

from backend.agent.message import AgentEvent


async def emit_command_error(
    session: Any,
    command: str,
    error: AgentEvent | BaseException | str,
    *,
    data: dict[str, Any] | None = None,
) -> None:
    details = dict(data or {})
    if isinstance(error, AgentEvent):
        message = str(error.data.get("message") or "Command failed")
        for key in ("error_type", "error_code", "recoverable", "provider_error_type"):
            if key in error.data:
                details.setdefault(key, error.data[key])
    else:
        message = str(error) or "Command failed"
    await session.send_event(
        AgentEvent.command_result(
            command,
            message,
            level="error",
            data=details or None,
        )
    )
