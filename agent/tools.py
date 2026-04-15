from collections.abc import Callable
from typing import Any


ToolHandler = Callable[[dict[str, Any]], str]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolHandler] = {}

    def register(self, name: str, handler: ToolHandler) -> None:
        self._tools[name] = handler

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def execute(self, name: str, payload: dict[str, Any]) -> str:
        return self._tools[name](payload)


def build_default_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()

    registry.register("echo", lambda payload: str(payload.get("text", "")))

    def summarize_text(payload: dict[str, Any]) -> str:
        text = str(payload.get("text", "")).strip()
        words = text.split()
        preview = " ".join(words[:5]) if words else "(empty)"
        return f"Summary({len(words)} words): {preview}"

    registry.register("summarize_text", summarize_text)

    return registry
