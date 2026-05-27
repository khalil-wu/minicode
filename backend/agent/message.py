"""Agent event and WebSocket command conversion helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.ws.events import ClientCommandType, ServerEventType


@dataclass
class AgentEvent:
    """Server-to-client event. See backend/ws/events.py for the full vocabulary."""

    type: ServerEventType
    data: dict[str, Any] = field(default_factory=dict)

    def to_ws_message(self) -> dict[str, Any]:
        """Serialize this event as WebSocket JSON."""
        return {"type": self.type, **self.data}

    @classmethod
    def text_chunk(cls, content: str) -> AgentEvent:
        return cls(type="text_chunk", data={"content": content})

    @classmethod
    def thinking_chunk(cls, content: str) -> AgentEvent:
        return cls(type="thinking_delta", data={"content": content})

    @classmethod
    def image_chunk(cls, data: str, media_type: str = "image/png") -> AgentEvent:
        return cls(type="image_chunk", data={"image_data": data, "media_type": media_type})

    @classmethod
    def tool_call(
        cls,
        id: str,
        name: str,
        args: dict[str, Any],
        *,
        status: str = "running",
        started_at: int | None = None,
        display_hint: str = "",
        input_summary: str = "",
    ) -> AgentEvent:
        data: dict[str, Any] = {
            "id": id,
            "name": name,
            "args": args,
            "status": status,
        }
        if started_at is not None:
            data["started_at"] = started_at
        if display_hint:
            data["display_hint"] = display_hint
        if input_summary:
            data["input_summary"] = input_summary
        return cls(type="tool_call", data=data)

    @classmethod
    def tool_result(
        cls,
        id: str,
        summary: str,
        artifact_id: str | None = None,
        is_error: bool = False,
        diff: Any | None = None,
        source_url: str | None = None,
        extraction_status: str | None = None,
        content_preview: str | None = None,
        evidence_type: str | None = None,
        status: str | None = None,
        duration_ms: int | None = None,
        display_summary: str = "",
        result_kind: str = "",
        limitation: str = "",
    ) -> AgentEvent:
        result: dict[str, Any] = {
            "id": id,
            "summary": summary,
            "is_error": is_error,
            "status": status or ("failed" if is_error else "success"),
        }
        if artifact_id:
            result["artifact_id"] = artifact_id
        if diff is not None:
            result["diff"] = diff
        if source_url:
            result["source_url"] = source_url
        if extraction_status:
            result["extraction_status"] = extraction_status
        if content_preview:
            result["content_preview"] = content_preview
        if evidence_type:
            result["evidence_type"] = evidence_type
        if duration_ms is not None:
            result["duration_ms"] = duration_ms
        if display_summary:
            result["display_summary"] = display_summary
        if result_kind:
            result["result_kind"] = result_kind
        if limitation:
            result["limitation"] = limitation
        return cls(type="tool_result", data=result)

    @classmethod
    def progress(
        cls,
        message: str,
        *,
        stage: str = "status",
        status: str = "info",
        id: str | None = None,
        detail: str = "",
        tool_call_id: str = "",
        tool_name: str = "",
        count: int | None = None,
        phase: str | None = None,
        label: str = "",
        summary: str = "",
        visibility: str = "timeline",
        group_id: str = "",
        step_id: str = "",
    ) -> AgentEvent:
        payload: dict[str, Any] = {
            "id": id or f"{stage}:{message}",
            "stage": stage,
            "status": status,
            "message": message,
            "phase": phase or stage,
            "label": label,
            "summary": summary or message,
            "visibility": visibility,
        }
        if detail:
            payload["detail"] = detail
        if tool_call_id:
            payload["tool_call_id"] = tool_call_id
        if tool_name:
            payload["tool_name"] = tool_name
        if count is not None:
            payload["count"] = count
        if group_id:
            payload["group_id"] = group_id
        if step_id:
            payload["step_id"] = step_id
        return cls(type="agent.progress", data=payload)

    @classmethod
    def approval_request(
        cls,
        tool_call_id: str,
        tool_name: str,
        args: dict[str, Any],
        diff: Any | None = None,
    ) -> AgentEvent:
        data: dict[str, Any] = {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "args": args,
        }
        if diff is not None:
            data["diff"] = diff
        return cls(type="approval_request", data=data)

    @classmethod
    def done(
        cls,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
    ) -> AgentEvent:
        return cls(
            type="done",
            data={
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_creation_input_tokens": cache_creation_input_tokens,
                    "cache_read_input_tokens": cache_read_input_tokens,
                }
            },
        )

    @classmethod
    def error(
        cls,
        message: str,
        recoverable: bool = True,
        error_type: str = "api",
        error_code: str = "",
    ) -> AgentEvent:
        data: dict[str, Any] = {
            "message": message,
            "recoverable": recoverable,
            "error_type": error_type,
        }
        if error_code:
            data["error_code"] = error_code
        return cls(type="error", data=data)

    @classmethod
    def approval_cancelled(
        cls,
        request_ids: list[str],
        *,
        reason: str = "run_cancelled",
    ) -> AgentEvent:
        return cls(
            type="approval.cancelled",
            data={"request_ids": request_ids, "reason": reason},
        )

    @classmethod
    def context_compacted(cls, summary: str) -> AgentEvent:
        return cls(type="context_compacted", data={"summary": summary})

    @classmethod
    def command_result(
        cls,
        command: str,
        message: str,
        *,
        level: str = "info",
        title: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> AgentEvent:
        payload: dict[str, Any] = {
            "command": command,
            "level": level,
            "message": message,
        }
        if title:
            payload["title"] = title
        if data:
            payload["data"] = data
        return cls(type="command.result", data=payload)

    @classmethod
    def plan_update(
        cls,
        plan_id: str,
        steps: list[dict[str, Any]],
        current_step: int = 0,
        status: str = "draft",
    ) -> AgentEvent:
        return cls(
            type="plan.update",
            data={
                "plan_id": plan_id,
                "steps": steps,
                "current_step": current_step,
                "status": status,
            },
        )

    @classmethod
    def task_update(
        cls, todo_id: str, status: str, content: str, active_form: str = ""
    ) -> AgentEvent:
        return cls(
            type="task.update",
            data={
                "todo_id": todo_id,
                "status": status,
                "content": content,
                "activeForm": active_form,
            },
        )

    @classmethod
    def subagent_start(
        cls, subagent_id: str, parent_id: str, role: str, prompt: str = ""
    ) -> AgentEvent:
        return cls(
            type="subagent.start",
            data={
                "subagent_id": subagent_id,
                "parent_id": parent_id,
                "role": role,
                "prompt": prompt,
            },
        )

    @classmethod
    def subagent_done(
        cls, subagent_id: str, summary: str = "", error: str = ""
    ) -> AgentEvent:
        data: dict[str, Any] = {"subagent_id": subagent_id, "summary": summary}
        if error:
            data["error"] = error
        return cls(type="subagent.done", data=data)

    @classmethod
    def budget_warning(
        cls, bucket: str, percent: float, will_compact: bool = False
    ) -> AgentEvent:
        return cls(
            type="budget.warning",
            data={
                "bucket": bucket,
                "percent": percent,
                "will_compact": will_compact,
            },
        )

    @classmethod
    def inspector_update(
        cls, target_kind: str, target_id: str, payload: dict[str, Any]
    ) -> AgentEvent:
        return cls(
            type="inspector.update",
            data={
                "target_kind": target_kind,
                "target_id": target_id,
                "payload": payload,
            },
        )


@dataclass
class UserCommand:
    """Client-to-server WebSocket message. See backend/ws/events.py."""

    type: ClientCommandType
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_ws_message(cls, msg: dict[str, Any]) -> UserCommand:
        """Deserialize a WebSocket JSON message."""
        msg_type = str(msg.get("type", "user_message"))
        data = {k: v for k, v in msg.items() if k != "type"}

        if msg_type == "control_cancel_request":
            request_id = data.get("request_id")
            if not request_id and "requestId" in data:
                data["request_id"] = data.pop("requestId")

        if msg_type == "control_response":
            if "request_id" not in data and "requestId" in data:
                data["request_id"] = data.pop("requestId")
            response = data.get("response")
            if isinstance(response, dict):
                if "request_id" not in response and "requestId" in response:
                    response = dict(response)
                    response["request_id"] = response.pop("requestId")
                data["response"] = response

        return cls(type=msg_type, data=data)
