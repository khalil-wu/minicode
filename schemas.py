from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolCallRecord(BaseModel):
    tool_name: str
    tool_input: dict[str, Any] = Field(default_factory=dict)
    tool_output: str | None = None
    status: Literal["success", "error"] = "success"


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    max_iterations: int = Field(default=3, ge=1, le=10)


class ChatResponse(BaseModel):
    reply: str
    stopped_reason: Literal[
        "completed",
        "tool_error",
        "invalid_model_action",
        "max_iterations",
    ]
    iterations: int
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
