from dataclasses import dataclass, field

from schemas import ToolCallRecord


@dataclass
class AgentState:
    user_message: str
    max_iterations: int
    iterations: int = 0
    reply: str = ""
    stopped_reason: str | None = None
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    tool_outputs: list[str] = field(default_factory=list)
