from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.agent.message import AgentEvent


def is_conversation_effort_command(data: dict[str, Any], conversation_id: str | None) -> bool:
    source = str(data.get("source") or "")
    return bool(conversation_id and "reasoning_effort" in data
                and (source == "frontend.footer" or source.startswith("slash:")))


@dataclass(frozen=True)
class ModelCommandRequest:
    model: str
    selection_mode: str = "model"
    reasoning_effort: str | None = None
    error_event: AgentEvent | None = None


def parse_model_command(data: dict[str, Any]) -> ModelCommandRequest:
    selection_mode = str(data.get("selection_mode", "model")).strip()
    if selection_mode not in {"model", "default"}:
        return ModelCommandRequest(model="", error_event=AgentEvent.error("Unknown model selection mode", recoverable=True))
    requested_model = str(data.get("model", "")).strip()
    if selection_mode == "model" and not requested_model:
        return ModelCommandRequest(
            model="",
            error_event=AgentEvent.error("Model name is required", recoverable=True),
        )
    return ModelCommandRequest(model=requested_model, selection_mode=selection_mode,
        reasoning_effort=str(data["reasoning_effort"]).strip().lower() if "reasoning_effort" in data else None)
