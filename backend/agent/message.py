"""
消息数据模型（DESIGN.md §一 agent/message.py）。

负责 LLM 消息格式 ↔ 内部事件格式 ↔ WebSocket 协议格式的互转。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


# ── WebSocket 协议类型（DESIGN.md §10）──────────────────────────

@dataclass
class AgentEvent:
    """
    Agent Loop 产出的事件（后端 → 前端）。

    对应 DESIGN.md §10 的 WebSocket 后端→前端协议。
    """

    type: Literal[
        "text_chunk",          # 文本片段
        "tool_call",           # 工具调用
        "tool_result",         # 工具执行结果
        "approval_request",    # 审批请求
        "skill_activated",     # Skill 激活
        "context_compacted",   # Compaction 发生
        "done",                # 完成
        "error",               # 错误
        "mcp_status",          # MCP 状态
    ]
    data: dict[str, Any] = field(default_factory=dict)

    def to_ws_message(self) -> dict[str, Any]:
        """序列化为 WebSocket JSON 消息。"""
        return {"type": self.type, **self.data}

    # ── 便捷工厂方法 ──

    @classmethod
    def text_chunk(cls, content: str) -> AgentEvent:
        return cls(type="text_chunk", data={"content": content})

    @classmethod
    def tool_call(
        cls, id: str, name: str, args: dict[str, Any]
    ) -> AgentEvent:
        return cls(
            type="tool_call",
            data={"id": id, "name": name, "args": args},
        )

    @classmethod
    def tool_result(
        cls,
        id: str,
        summary: str,
        artifact_id: str | None = None,
    ) -> AgentEvent:
        result: dict[str, Any] = {"id": id, "summary": summary}
        if artifact_id:
            result["artifact_id"] = artifact_id
        return cls(type="tool_result", data=result)

    @classmethod
    def approval_request(
        cls,
        tool_call_id: str,
        tool_name: str,
        args: dict[str, Any],
        diff: str | None = None,
    ) -> AgentEvent:
        data: dict[str, Any] = {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "args": args,
        }
        if diff:
            data["diff"] = diff
        return cls(type="approval_request", data=data)

    @classmethod
    def done(cls, input_tokens: int = 0, output_tokens: int = 0) -> AgentEvent:
        return cls(
            type="done",
            data={
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                }
            },
        )

    @classmethod
    def error(
        cls,
        message: str,
        recoverable: bool = True,
        error_type: str = "api",
    ) -> AgentEvent:
        return cls(
            type="error",
            data={
                "message": message,
                "recoverable": recoverable,
                "error_type": error_type,
            },
        )

    @classmethod
    def context_compacted(cls, summary: str) -> AgentEvent:
        return cls(
            type="context_compacted",
            data={"summary": summary},
        )


# ── 前端 → 后端消息类型（DESIGN.md §10）──────────────────────────

@dataclass
class UserCommand:
    """前端发来的 WebSocket 消息。"""

    type: Literal[
        "user_message",
        "approval",
        "interrupt",
        "load_skill",
    ]
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_ws_message(cls, msg: dict[str, Any]) -> UserCommand:
        """从 WebSocket JSON 消息反序列化。"""
        msg_type = msg.get("type", "user_message")
        data = {k: v for k, v in msg.items() if k != "type"}
        return cls(type=msg_type, data=data)
