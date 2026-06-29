"""Agent event and WebSocket command conversion helpers.

Output-protocol note (vs. Codex): Codex's wire shape carries `phase` as a field
on a structured `message` output item (`{type:"message", phase:"commentary"|"final_answer"}`).
MiniCode instead attaches `phase`/`visibility` as metadata on `text_chunk` events
(see `text_chunk` below) and projects them into process/answer channels on the
frontend (`frontend/src.v2/lib/turn-projection.ts`). The two are functionally
equivalent — both separate in-progress narration from the final answer — and a
full migration to Codex-style structured message items is intentionally deferred
(wire-protocol + projection + test rewrite for no functional gain). Do not
"fix" this difference without revisiting that decision.
"""

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
    def text_chunk(
        cls,
        content: str,
        *,
        source: str = "",
        visibility: str = "",
        role: str = "",
        phase: str = "",
        finalize: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> AgentEvent:
        data: dict[str, Any] = {"content": content}
        if source:
            data["source"] = source
        if visibility:
            data["visibility"] = visibility
        if role:
            data["role"] = role
        if phase:
            data["phase"] = phase
        if finalize:
            # Contentless seal: re-tag the last streamed text block as the
            # final answer without re-emitting content (already streamed live).
            data["finalize"] = True
        if metadata:
            data["metadata"] = metadata
        return cls(type="text_chunk", data=data)

    @classmethod
    def text_replace(cls, content: str = "") -> AgentEvent:
        return cls(type="text_replace", data={"content": content})

    @classmethod
    def thinking_chunk(
        cls,
        content: str,
        *,
        source: str = "",
        visibility: str = "",
        is_raw_provider_reasoning: bool = False,
        provider_reasoning_type: str = "",
        phase: str = "",
    ) -> AgentEvent:
        data: dict[str, Any] = {"content": content}
        if source:
            data["source"] = source
        if visibility:
            data["visibility"] = visibility
        if is_raw_provider_reasoning:
            data["is_raw_provider_reasoning"] = True
        if provider_reasoning_type:
            data["provider_reasoning_type"] = provider_reasoning_type
        if phase:
            data["phase"] = phase
        return cls(type="thinking_delta", data=data)

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
        result_kind: str = "",
        activity_kind: str = "",
        group_id: str = "",
        step_id: str = "",
        turn_id: str = "",
        iteration_id: str = "",
        phase: str = "",
        display_scope: str = "activity",
        panel_hint: str = "inspector",
        requires_attention: bool = False,
    ) -> AgentEvent:
        data: dict[str, Any] = {
            "id": id,
            "name": name,
            "args": args,
            "status": status,
            "display_scope": display_scope,
            "panel_hint": panel_hint,
            "requires_attention": requires_attention,
        }
        if started_at is not None:
            data["started_at"] = started_at
        if display_hint:
            data["display_hint"] = display_hint
        if input_summary:
            data["input_summary"] = input_summary
        if result_kind:
            data["result_kind"] = result_kind
        if activity_kind:
            data["activity_kind"] = activity_kind
        if group_id:
            data["group_id"] = group_id
        if step_id:
            data["step_id"] = step_id
        if turn_id:
            data["turn_id"] = turn_id
        if iteration_id:
            data["iteration_id"] = iteration_id
        if phase:
            data["phase"] = phase
        return cls(type="tool_call", data=data)

    @classmethod
    def tool_output_delta(
        cls,
        id: str,
        output: str,
        *,
        stream: str = "stdout",
        turn_id: str = "",
        iteration_id: str = "",
        step_id: str = "",
    ) -> AgentEvent:
        """工具执行期间的增量输出（如命令的 stdout/stderr）。"""
        data = {"id": id, "output": output, "stream": stream}
        if turn_id:
            data["turn_id"] = turn_id
        if iteration_id:
            data["iteration_id"] = iteration_id
        if step_id:
            data["step_id"] = step_id
        return cls(
            type="tool_output_delta",
            data=data,
        )

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
        activity_kind: str = "",
        group_id: str = "",
        step_id: str = "",
        limitation: str = "",
        provider: str = "",
        provider_error_type: str = "",
        error_info: dict[str, Any] | None = None,
        error_kind: str = "",
        user_summary: str = "",
        developer_detail: str = "",
        recoverable: bool | None = None,
        projection: str = "",
        turn_id: str = "",
        iteration_id: str = "",
        phase: str = "",
        display_scope: str = "activity",
        panel_hint: str = "inspector",
        requires_attention: bool = False,
    ) -> AgentEvent:
        result: dict[str, Any] = {
            "id": id,
            "summary": summary,
            "is_error": is_error,
            "status": status or ("failed" if is_error else "success"),
            "display_scope": display_scope,
            "panel_hint": panel_hint,
            "requires_attention": requires_attention,
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
        if group_id:
            result["group_id"] = group_id
        if step_id:
            result["step_id"] = step_id
        if limitation:
            result["limitation"] = limitation
        if provider:
            result["provider"] = provider
        if provider_error_type:
            result["provider_error_type"] = provider_error_type
        if error_info:
            result["error_info"] = error_info
        if error_kind:
            result["error_kind"] = error_kind
        if user_summary:
            result["user_summary"] = user_summary
        if developer_detail:
            result["developer_detail"] = developer_detail
        if recoverable is not None:
            result["recoverable"] = recoverable
        if projection:
            result["projection"] = projection
        if turn_id:
            result["turn_id"] = turn_id
        if iteration_id:
            result["iteration_id"] = iteration_id
        if phase:
            result["phase"] = phase
        return cls(type="tool_result", data=result)

    @classmethod
    def loop_started(
        cls,
        *,
        loop_id: str,
        iteration_id: str = "",
        title: str = "Working",
        summary: str = "",
        started_at: int | None = None,
        default_collapsed: bool = False,
    ) -> AgentEvent:
        payload: dict[str, Any] = {
            "item_id": loop_id,
            "loop_id": loop_id,
            "iteration_id": iteration_id or loop_id,
            "status": "running",
            "title": title,
            "summary": summary or title,
            "default_collapsed": default_collapsed,
        }
        if started_at is not None:
            payload["started_at"] = started_at
        return cls(type="agent.loop.started", data=payload)

    @classmethod
    def loop_completed(
        cls,
        *,
        loop_id: str,
        iteration_id: str = "",
        status: str = "completed",
        title: str = "Processed",
        summary: str = "",
        started_at: int | None = None,
        completed_at: int | None = None,
        duration_ms: int | None = None,
        item_count: int | None = None,
        tool_call_count: int | None = None,
        default_collapsed: bool = True,
    ) -> AgentEvent:
        payload: dict[str, Any] = {
            "item_id": loop_id,
            "loop_id": loop_id,
            "iteration_id": iteration_id or loop_id,
            "status": status,
            "title": title,
            "summary": summary or title,
            "default_collapsed": default_collapsed,
        }
        if started_at is not None:
            payload["started_at"] = started_at
        if completed_at is not None:
            payload["completed_at"] = completed_at
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms
        if item_count is not None:
            payload["item_count"] = item_count
        if tool_call_count is not None:
            payload["tool_call_count"] = tool_call_count
        return cls(type="agent.loop.completed", data=payload)

    @classmethod
    def agent_item(
        cls,
        *,
        id: str,
        kind: str,
        content: str = "",
        loop_id: str = "",
        iteration_id: str = "",
        parent_id: str = "",
        role: str = "assistant",
        source: str = "",
        status: str = "completed",
        title: str = "",
        summary: str = "",
        visibility: str = "timeline",
        created_at: int | None = None,
        order: int | None = None,
        seq: int | None = None,
        default_collapsed: bool | None = None,
        group_id: str = "",
        step_id: str = "",
        tool_call_ids: list[str] | None = None,
        display_scope: str = "",
        panel_hint: str = "",
        requires_attention: bool = False,
        skill_name: str = "",
        trigger_mode: str = "",
        source_level: str = "",
        reason: str = "",
        token_estimate: int | None = None,
    ) -> AgentEvent:
        payload: dict[str, Any] = {
            "id": id,
            "item_id": id,
            "kind": kind,
            "role": role,
            "status": status,
            "visibility": visibility,
            "requires_attention": requires_attention,
        }
        if display_scope:
            payload["display_scope"] = display_scope
        if panel_hint:
            payload["panel_hint"] = panel_hint
        if source:
            payload["source"] = source
        if content:
            payload["content"] = content
        if loop_id:
            payload["loop_id"] = loop_id
        if iteration_id:
            payload["iteration_id"] = iteration_id
        if parent_id:
            payload["parent_id"] = parent_id
        if title:
            payload["title"] = title
        if summary:
            payload["summary"] = summary
        if created_at is not None:
            payload["created_at"] = created_at
        if order is not None:
            payload["order"] = order
        if seq is not None:
            payload["seq"] = seq
        if default_collapsed is not None:
            payload["default_collapsed"] = default_collapsed
        if group_id:
            payload["group_id"] = group_id
        if step_id:
            payload["step_id"] = step_id
        if tool_call_ids:
            payload["tool_call_ids"] = tool_call_ids
        if skill_name:
            payload["skill_name"] = skill_name
        if trigger_mode:
            payload["trigger_mode"] = trigger_mode
        if source_level:
            payload["source_level"] = source_level
        if reason:
            payload["reason"] = reason
        if token_estimate is not None:
            payload["token_estimate"] = token_estimate
        return cls(type="agent.item", data=payload)

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
        iteration_id: str = "",
        display_scope: str = "",
        panel_hint: str = "",
        requires_attention: bool = False,
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
            "requires_attention": requires_attention,
        }
        if display_scope:
            payload["display_scope"] = display_scope
        if panel_hint:
            payload["panel_hint"] = panel_hint
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
        if iteration_id:
            payload["iteration_id"] = iteration_id
        return cls(type="agent.progress", data=payload)

    @classmethod
    def agent_run_started(cls, record: Any) -> AgentEvent:
        payload = record.to_dict() if hasattr(record, "to_dict") else dict(record or {})
        return cls(type="agent.run.started", data=payload)

    @classmethod
    def agent_run_updated(cls, record: Any) -> AgentEvent:
        payload = record.to_dict() if hasattr(record, "to_dict") else dict(record or {})
        return cls(type="agent.run.updated", data=payload)

    @classmethod
    def agent_run_completed(cls, record: Any) -> AgentEvent:
        payload = record.to_dict() if hasattr(record, "to_dict") else dict(record or {})
        return cls(type="agent.run.completed", data=payload)

    @classmethod
    def agent_phase_updated(
        cls,
        run_id: str,
        phase: str,
        *,
        status: str = "running",
        summary: str = "",
        role: str = "",
        conversation_id: str = "",
    ) -> AgentEvent:
        payload: dict[str, Any] = {
            "run_id": run_id,
            "phase": phase,
            "status": status,
        }
        if summary:
            payload["summary"] = summary
        if role:
            payload["role"] = role
        if conversation_id:
            payload["conversation_id"] = conversation_id
        return cls(type="agent.phase.updated", data=payload)

    @classmethod
    def verification_started(
        cls,
        run_id: str,
        *,
        command: str = "",
        conversation_id: str = "",
    ) -> AgentEvent:
        payload: dict[str, Any] = {"run_id": run_id}
        payload["display_scope"] = "activity"
        payload["panel_hint"] = "plan"
        payload["requires_attention"] = False
        if command:
            payload["command"] = command
        if conversation_id:
            payload["conversation_id"] = conversation_id
        return cls(type="verification.started", data=payload)

    @classmethod
    def verification_result(
        cls,
        run_id: str,
        *,
        passed: bool,
        output: str = "",
        command: str = "",
        conversation_id: str = "",
    ) -> AgentEvent:
        payload: dict[str, Any] = {"run_id": run_id, "passed": passed}
        payload["display_scope"] = "activity" if passed else "notice"
        payload["panel_hint"] = "plan"
        payload["requires_attention"] = not passed
        if output:
            payload["output"] = output
        if command:
            payload["command"] = command
        if conversation_id:
            payload["conversation_id"] = conversation_id
        return cls(type="verification.result", data=payload)

    @classmethod
    def plan_step_updated(
        cls,
        plan_id: str,
        status: str,
        *,
        step_id: str = "",
        step_index: int | None = None,
        title: str = "",
        detail: str = "",
        current_step: int | None = None,
    ) -> AgentEvent:
        data: dict[str, Any] = {"plan_id": plan_id, "status": status}
        if step_id:
            data["step_id"] = step_id
        if step_index is not None:
            data["step_index"] = step_index
        if title:
            data["title"] = title
        if detail:
            data["detail"] = detail
        if current_step is not None:
            data["current_step"] = current_step
        return cls(type="plan_step_updated", data=data)

    @classmethod
    def plan_updated(
        cls,
        plan_id: str,
        steps: list[dict[str, Any]],
        *,
        status: str = "executing",
        current_step: int = 0,
        explanation: str = "",
    ) -> AgentEvent:
        """Full-plan snapshot emitted by the update_plan tool.

        Carries the entire step list so the frontend can create or replace the
        live plan in one event; per-step progress still flows via
        plan_step_updated.
        """
        data: dict[str, Any] = {
            "plan_id": plan_id,
            "status": status,
            "steps": steps,
            "current_step": current_step,
        }
        if explanation:
            data["explanation"] = explanation
        return cls(type="plan_updated", data=data)

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
    def permission_decision(
        cls,
        *,
        tool_call_id: str,
        tool_name: str,
        decision: str,
        source: str = "hook",
        permission_level: str = "",
        message: str = "",
    ) -> AgentEvent:
        data: dict[str, Any] = {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "decision": decision,
            "source": source,
        }
        if permission_level:
            data["permission_level"] = permission_level
        if message:
            data["message"] = message
        return cls(type="permission.decision", data=data)

    @classmethod
    def done(
        cls,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
        reasoning_output_tokens: int = 0,
        provider_raw: dict[str, Any] | None = None,
    ) -> AgentEvent:
        usage = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": cache_creation_input_tokens,
            "cache_read_input_tokens": cache_read_input_tokens,
        }
        if reasoning_output_tokens:
            usage["reasoning_output_tokens"] = reasoning_output_tokens
        data: dict[str, Any] = {"usage": usage}
        if provider_raw:
            data["providerRaw"] = provider_raw
        return cls(
            type="done",
            data=data,
        )

    @classmethod
    def error(
        cls,
        message: str,
        recoverable: bool = True,
        error_type: str = "api",
        error_code: str = "",
        provider_error_type: str = "",
    ) -> AgentEvent:
        data: dict[str, Any] = {
            "message": message,
            "recoverable": recoverable,
            "error_type": error_type,
        }
        if error_code:
            data["error_code"] = error_code
        if provider_error_type:
            data["provider_error_type"] = provider_error_type
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
    def stream_resume(
        cls,
        conversation_id: str,
        message_id: str | None,
        tool_calls_pending: list[dict[str, Any]] | None = None,
        accumulated_text: str = "",
    ) -> AgentEvent:
        return cls(
            type="stream_resume",
            data={
                "conversation_id": conversation_id,
                "message_id": message_id,
                "tool_calls_pending": tool_calls_pending or [],
                "accumulated_text": accumulated_text,
            },
        )

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
                "display_scope": "agents",
                "panel_hint": "subagents",
                "requires_attention": False,
            },
        )

    @classmethod
    def subagent_progress(
        cls,
        subagent_id: str,
        *,
        iteration: int = 0,
        max_iterations: int = 0,
        tool_name: str = "",
        detail: str = "",
    ) -> AgentEvent:
        """Emitted during subagent execution to report intermediate progress."""
        data: dict[str, Any] = {
            "subagent_id": subagent_id,
            "iteration": iteration,
            "display_scope": "agents",
            "panel_hint": "subagents",
            "requires_attention": False,
        }
        if max_iterations:
            data["max_iterations"] = max_iterations
        if tool_name:
            data["tool_name"] = tool_name
        if detail:
            data["detail"] = detail
        return cls(type="subagent.progress", data=data)

    @classmethod
    def subagent_done(
        cls,
        subagent_id: str,
        summary: str = "",
        error: str = "",
        *,
        duration_ms: int | None = None,
        iterations: int = 0,
        tool_call_count: int = 0,
        timed_out: bool = False,
    ) -> AgentEvent:
        data: dict[str, Any] = {
            "subagent_id": subagent_id,
            "summary": summary,
            "display_scope": "agents",
            "panel_hint": "subagents",
            "requires_attention": bool(error or timed_out),
        }
        if error:
            data["error"] = error
        if duration_ms is not None:
            data["duration_ms"] = duration_ms
        if iterations:
            data["iterations"] = iterations
        if tool_call_count:
            data["tool_call_count"] = tool_call_count
        if timed_out:
            data["timed_out"] = True
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
        cls,
        target_kind: str,
        target_id: str,
        payload: dict[str, Any],
        *,
        display_scope: str = "",
        panel_hint: str = "",
        requires_attention: bool = False,
    ) -> AgentEvent:
        data: dict[str, Any] = {
            "target_kind": target_kind,
            "target_id": target_id,
            "payload": payload,
        }
        if display_scope:
            data["display_scope"] = display_scope
        if panel_hint:
            data["panel_hint"] = panel_hint
        if requires_attention:
            data["requires_attention"] = True
        return cls(
            type="inspector.update",
            data=data,
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
