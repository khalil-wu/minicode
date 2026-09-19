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
    error_event: AgentEvent | None = None


def parse_model_command(data: dict[str, Any]) -> ModelCommandRequest:
    requested_model = str(data.get("model", "")).strip()
    if not requested_model:
        return ModelCommandRequest(
            model="",
            error_event=AgentEvent.error("Model name is required", recoverable=True),
        )
    return ModelCommandRequest(model=requested_model)
