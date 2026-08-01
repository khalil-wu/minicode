"""Agent event and WebSocket command conversion helpers.

Provider text with an explicit phase is projected at its source. Providers that
do not expose phases are resolved within one provider response: text preceding
a tool call is process output, while text from a response without tools is the
answer. Assistant text follows the same item lifecycle used by Codex and pi:
started, delta, completed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from backend.agent.context_ledger import ContextLedger
from backend.agent.prompt_cache import prompt_cache_usage_stats
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
    def user_message_queue_updated(
        cls,
        *,
        status: str,
        conversation_id: str,
        message_id: str,
        user_message_id: str = "",
        position: int = 0,
        reason: str = "",
        target_message_id: str = "",
        turn_mode: str = "",
    ) -> AgentEvent:
        data: dict[str, Any] = {
            "status": status,
            "conversation_id": conversation_id,
            "message_id": message_id,
        }
        if user_message_id:
            data["user_message_id"] = user_message_id
        if position > 0:
            data["position"] = position
        if reason:
            data["reason"] = reason
        if target_message_id:
            data["target_message_id"] = target_message_id
        if turn_mode:
            data["turn_mode"] = turn_mode
        return cls(type="user_message.queue.updated", data=data)

    @classmethod
    def agent_message_started(
        cls,
        *,
        item_id: str = "agent-message",
    ) -> AgentEvent:
        return cls(type="item.started", data={
            "item": {
                "id": item_id,
                "type": "agent_message",
                "text": "",
                "status": "in_progress",
            },
        })

    @classmethod
    def agent_message_delta(
        cls,
        delta: str,
        *,
        item_id: str = "agent-message",
    ) -> AgentEvent:
        return cls(type="agent_message.delta", data={
            "item_id": item_id,
            "delta": delta,
        })

    @classmethod
    def agent_message_completed(
        cls,
        text: str,
        *,
        item_id: str = "agent-message",
        source: str = "model_final",
        status: str = "completed",
        finish_reason: str = "",
        provider_raw: dict[str, Any] | None = None,
    ) -> AgentEvent:
        item: dict[str, Any] = {
            "id": item_id,
            "type": "agent_message",
            "text": text,
            "source": source,
            "status": status,
        }
        data: dict[str, Any] = {"item": item}
        if finish_reason:
            data["finish_reason"] = finish_reason
        if provider_raw:
            data["provider_raw"] = dict(provider_raw)
        return cls(type="item.completed", data=data)

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
        result_kind: str = "",
        activity_kind: str = "",
        group_id: str = "",
        step_id: str = "",
        turn_id: str = "",
        iteration_id: str = "",
        phase: str = "",
        side_effect_kind: str = "",
        idempotent: bool | None = None,
        idempotency_key: str = "",
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
        if side_effect_kind:
            data["side_effect_kind"] = side_effect_kind
        if idempotent is not None:
            data["idempotent"] = idempotent
        if idempotency_key:
            data["idempotency_key"] = idempotency_key
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
        side_effect_kind: str = "",
        idempotent: bool | None = None,
        idempotency_key: str = "",
        output_files: list[dict[str, Any]] | None = None,
        superseded_tool_call_ids: list[str] | None = None,
        removed_file_paths: list[str] | None = None,
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
        if activity_kind:
            result["activity_kind"] = activity_kind
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
        if side_effect_kind:
            result["side_effect_kind"] = side_effect_kind
        if idempotent is not None:
            result["idempotent"] = idempotent
        if idempotency_key:
            result["idempotency_key"] = idempotency_key
        if output_files:
            result["output_files"] = [dict(item) for item in output_files]
        if superseded_tool_call_ids:
            result["superseded_tool_call_ids"] = list(superseded_tool_call_ids)
        if removed_file_paths:
            result["removed_file_paths"] = list(removed_file_paths)
        return cls(type="tool_result", data=result)

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
        }
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
        ephemeral: bool = False,
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
        if ephemeral:
            payload["ephemeral"] = True
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
        cls._add_canonical_lifecycle(payload, kind="agent")
        return cls(type="agent.run.started", data=payload)

    @classmethod
    def agent_run_completed(cls, record: Any) -> AgentEvent:
        payload = record.to_dict() if hasattr(record, "to_dict") else dict(record or {})
        cls._add_canonical_lifecycle(payload, kind="agent")
        return cls(type="agent.run.completed", data=payload)

    @staticmethod
    def _add_canonical_lifecycle(payload: dict[str, Any], *, kind: str) -> None:
        run_id = str(payload.get("run_id") or payload.get("subagent_id") or "")
        mailbox_epoch = int(payload.get("mailbox_epoch") or 0)
        agent_path = str(payload.get("agent_path") or "")
        owner = str(payload.get("runtime_owner_token") or "")
        payload.setdefault("run_id", run_id)
        payload.setdefault("task_id", str(payload.get("task_id") or run_id))
        payload.setdefault("parent_run_id", str(payload.get("parent_run_id") or payload.get("parent_id") or ""))
        payload.setdefault("incarnation", f"{agent_path or run_id}:{mailbox_epoch}:{owner}")
        payload.setdefault("kind", kind)
        payload.setdefault("phase", str(payload.get("phase") or "running"))
        payload.setdefault("status", str(payload.get("status") or "running"))
        payload.setdefault("updated_at", int(payload.get("completed_at") or payload.get("last_progress_at") or time.time() * 1000))
        payload.setdefault("result", {})

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
        source_agent: str = "",
        source_thread: str = "",
        source_tool: str = "",
    ) -> AgentEvent:
        data: dict[str, Any] = {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "args": args,
        }
        if source_agent:
            data["source_agent"] = source_agent
        if source_thread:
            data["source_thread"] = source_thread
        if source_tool:
            data["source_tool"] = source_tool
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
        capability: dict[str, Any] | None = None,
        approval_policy: str = "",
        matched_rule: dict[str, str] | None = None,
        risk: str = "",
        scope: dict[str, Any] | None = None,
        expiry: str = "",
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
        if capability:
            data["capability"] = capability
        if approval_policy:
            data["approval_policy"] = approval_policy
        if matched_rule:
            data["matched_rule"] = matched_rule
        if risk:
            data["risk"] = risk
        if scope:
            data["scope"] = scope
        if expiry:
            data["expiry"] = expiry
        return cls(type="permission.decision", data=data)

    @classmethod
    def done(
        cls,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
        cache_deleted_input_tokens: int = 0,
        reasoning_output_tokens: int = 0,
        input_includes_cache_read: bool = True,
        provider_raw: dict[str, Any] | None = None,
        status: str = "completed",
        reason: str = "",
        duration_ms: int | None = None,
    ) -> AgentEvent:
        usage = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": cache_creation_input_tokens,
            "cache_read_input_tokens": cache_read_input_tokens,
            "input_includes_cache_read": bool(input_includes_cache_read),
        }
        if cache_deleted_input_tokens:
            usage["cache_deleted_input_tokens"] = cache_deleted_input_tokens
        if reasoning_output_tokens:
            usage["reasoning_output_tokens"] = reasoning_output_tokens
        if cache_read_input_tokens or cache_creation_input_tokens:
            usage.update(prompt_cache_usage_stats(usage, provider_raw))
        normalized_status = status if status in {"completed", "partial", "cancelled", "failed"} else "completed"
        data: dict[str, Any] = {"usage": usage, "status": normalized_status}
        if reason:
            data["reason"] = reason
        if duration_ms is not None:
            data["duration_ms"] = max(0, int(duration_ms))
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
    def context_compacted(
        cls,
        summary: str,
        *,
        before_tokens: int | None = None,
        after_tokens: int | None = None,
        retained_categories: list[str] | None = None,
        ledger: ContextLedger | None = None,
    ) -> AgentEvent:
        data: dict[str, Any] = {"summary": summary}
        if before_tokens is not None:
            data["before_tokens"] = max(0, int(before_tokens))
        if after_tokens is not None:
            data["after_tokens"] = max(0, int(after_tokens))
        if retained_categories is not None:
            data["retained_categories"] = retained_categories
        if ledger is not None:
            data["ledger"] = ledger
        return cls(type="context_compacted", data=data)

    @classmethod
    def stream_resume(
        cls,
        conversation_id: str,
        message_id: str | None,
        tool_calls_pending: list[dict[str, Any]] | None = None,
        content_blocks: list[dict[str, Any]] | None = None,
    ) -> AgentEvent:
        return cls(
            type="stream_resume",
            data={
                "conversation_id": conversation_id,
                "message_id": message_id,
                "tool_calls_pending": tool_calls_pending or [],
                "content_blocks": content_blocks or [],
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
        cls,
        subagent_id: str,
        parent_id: str,
        role: str,
        prompt: str = "",
        *,
        current_activity: str = "",
        waiting_on: str = "",
        last_progress_at: int | None = None,
        agent_path: str = "",
        mailbox_epoch: int | None = None,
    ) -> AgentEvent:
        data: dict[str, Any] = {
            "subagent_id": subagent_id,
            "parent_id": parent_id,
            "role": role,
            "prompt": prompt,
        }
        if current_activity:
            data["current_activity"] = current_activity
        if waiting_on:
            data["waiting_on"] = waiting_on
        if last_progress_at is not None:
            data["last_progress_at"] = last_progress_at
        if agent_path:
            data["agent_path"] = agent_path
        if mailbox_epoch is not None:
            data["mailbox_epoch"] = max(0, int(mailbox_epoch))
        cls._add_canonical_lifecycle(data, kind="subagent")
        return cls(type="subagent.start", data=data)

    @classmethod
    def subagent_progress(
        cls,
        subagent_id: str,
        *,
        iteration: int = 0,
        max_iterations: int = 0,
        tool_name: str = "",
        detail: str = "",
        current_activity: str = "",
        waiting_on: str = "",
        last_progress_at: int | None = None,
        activity_kind: str = "",
        activity_summary: str = "",
        user_visible: bool | None = None,
        agent_path: str = "",
        mailbox_epoch: int | None = None,
    ) -> AgentEvent:
        """Emitted during subagent execution to report intermediate progress."""
        data: dict[str, Any] = {
            "subagent_id": subagent_id,
            "iteration": iteration,
        }
        if max_iterations:
            data["max_iterations"] = max_iterations
        if tool_name:
            data["tool_name"] = tool_name
        if detail:
            data["detail"] = detail
        if current_activity:
            data["current_activity"] = current_activity
        if waiting_on:
            data["waiting_on"] = waiting_on
        if last_progress_at is not None:
            data["last_progress_at"] = last_progress_at
        if activity_kind:
            data["activity_kind"] = activity_kind
        if activity_summary:
            data["activity_summary"] = activity_summary
        if user_visible is not None:
            data["user_visible"] = user_visible
        if agent_path:
            data["agent_path"] = agent_path
        if mailbox_epoch is not None:
            data["mailbox_epoch"] = max(0, int(mailbox_epoch))
        cls._add_canonical_lifecycle(data, kind="subagent")
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
        status: str = "completed",
        termination_reason: str = "success",
        initiator: str = "runtime",
        usage: dict[str, Any] | None = None,
        agent_path: str = "",
        mailbox_epoch: int | None = None,
    ) -> AgentEvent:
        data: dict[str, Any] = {
            "subagent_id": subagent_id,
            "summary": summary,
            "status": status,
            "termination_reason": termination_reason,
            "initiator": initiator,
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
        if usage:
            data["usage"] = dict(usage)
        if agent_path:
            data["agent_path"] = agent_path
        if mailbox_epoch is not None:
            data["mailbox_epoch"] = max(0, int(mailbox_epoch))
        data["phase"] = "completed"
        data["result"] = {"summary": summary} if summary else {}
        cls._add_canonical_lifecycle(data, kind="subagent")
        return cls(type="subagent.done", data=data)

    @classmethod
    def stream_event(
        cls,
        *,
        provider: str,
        event_type: str,
        data: dict[str, Any],
        sdk_only: bool = True,
    ) -> AgentEvent:
        """Raw provider stream event passthrough for SDK consumers.

        When sdk_only is True (default), the UI should not render this — it's
        intended for programmatic consumers that need access to the underlying
        provider stream (e.g. RawMessageStreamEvent from Anthropic SDK).
        """
        return cls(
            type="stream_event",
            data={
                "provider": provider,
                "event_type": event_type,
                "data": data,
                "sdk_only": sdk_only,
            },
        )

    @classmethod
    def rate_limit(
        cls,
        *,
        provider: str = "",
        error_type: str = "rate_limit",
        retry_after_seconds: float = 0.0,
        message: str = "",
        recoverable: bool = True,
    ) -> AgentEvent:
        import time as _time
        data: dict[str, Any] = {
            "provider": provider,
            "error_type": error_type,
            "recoverable": recoverable,
        }
        if retry_after_seconds > 0:
            data["retry_after_seconds"] = retry_after_seconds
            data["retry_at"] = int(_time.time() * 1000) + int(retry_after_seconds * 1000)
        if message:
            data["message"] = message
        return cls(type="rate_limit", data=data)

    @classmethod
    def session_state_changed(
        cls,
        *,
        state: str,
        conversation_id: str = "",
        run_id: str = "",
        reason: str = "",
    ) -> AgentEvent:
        data: dict[str, Any] = {"state": state}
        if conversation_id:
            data["conversation_id"] = conversation_id
        if run_id:
            data["run_id"] = run_id
        if reason:
            data["reason"] = reason
        return cls(type="session.state_changed", data=data)

    @classmethod
    def tool_use_summary(
        cls,
        *,
        summary: str,
        iteration_id: str = "",
        tool_call_ids: list[str] | None = None,
        tool_count: int = 0,
        generated_by: str = "runtime",
    ) -> AgentEvent:
        data: dict[str, Any] = {
            "summary": summary,
            "generated_by": generated_by,
        }
        if iteration_id:
            data["iteration_id"] = iteration_id
        if tool_call_ids:
            data["tool_call_ids"] = tool_call_ids
        if tool_count:
            data["tool_count"] = tool_count
        return cls(type="tool_use_summary", data=data)

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
    ) -> AgentEvent:
        data: dict[str, Any] = {
            "target_kind": target_kind,
            "target_id": target_id,
            "payload": payload,
        }
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
