from __future__ import annotations

from typing import Any

from backend.agent.message import AgentEvent


def short_text(value: Any, limit: int = 72) -> str:
    text = str(value or "").replace("\n", " ").strip()
    return text if len(text) <= limit else f"{text[:limit - 1]}..."


def _arg(args: dict[str, Any], *keys: str, limit: int = 72) -> str:
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return short_text(value.strip(), limit)
    return ""


def agent_progress(
    message: str,
    *,
    stage: str,
    status: str = "running",
    id: str,
    phase: str,
    label: str,
    summary: str | None = None,
    detail: str = "",
    visibility: str = "timeline",
    count: int | None = None,
    group_id: str = "",
    step_id: str = "",
) -> AgentEvent:
    return AgentEvent.progress(
        message,
        stage=stage,
        status=status,
        id=id,
        phase=phase,
        label=label,
        summary=summary or message,
        detail=detail,
        visibility=visibility,
        count=count,
        group_id=group_id,
        step_id=step_id,
    )
