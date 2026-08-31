from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.agent.message import AgentEvent


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
