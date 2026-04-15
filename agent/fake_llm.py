from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class ModelDecision:
    action: Literal["respond", "tool_call"]
    response_text: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] = field(default_factory=dict)


class FakeLLM:
    def decide(self, user_message: str, tool_outputs: list[str]) -> ModelDecision:
        lowered = user_message.lower()

        if tool_outputs:
            return ModelDecision(
                action="respond",
                response_text=f"I used a tool and got: {tool_outputs[-1]}",
            )

        if lowered.startswith("use echo:"):
            text = user_message.split(":", 1)[1].strip()
            return ModelDecision(
                action="tool_call",
                tool_name="echo",
                tool_input={"text": text},
            )

        if lowered.startswith("summarize:"):
            text = user_message.split(":", 1)[1].strip()
            return ModelDecision(
                action="tool_call",
                tool_name="summarize_text",
                tool_input={"text": text},
            )

        if lowered.startswith("use missing tool:"):
            return ModelDecision(
                action="tool_call",
                tool_name="missing_tool",
                tool_input={"text": user_message},
            )

        return ModelDecision(
            action="respond",
            response_text=f"Direct response: {user_message}",
        )
