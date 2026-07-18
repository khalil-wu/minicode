from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.agent.message import AgentEvent


@dataclass(frozen=True)
class ModelCommandRequest:
    model: str
    error_event: AgentEvent | None = None


def build_inspector_focus_event(data: dict[str, Any], *, conversation_id: str = "") -> AgentEvent:
    target_kind = str(data.get("target_kind", "")).strip() or "message"
    target_id = str(data.get("target_id", "")).strip()
    event = AgentEvent.inspector_update(target_kind=target_kind, target_id=target_id, payload={"acknowledged": True})
    clean_conversation_id = str(conversation_id or "").strip()
    if clean_conversation_id:
        event.data["conversation_id"] = clean_conversation_id
    return event


def parse_model_command(data: dict[str, Any]) -> ModelCommandRequest:
    requested_model = str(data.get("model", "")).strip()
    if not requested_model:
        return ModelCommandRequest(
            model="",
            error_event=AgentEvent.error("Model name is required", recoverable=True),
        )
    return ModelCommandRequest(model=requested_model)
