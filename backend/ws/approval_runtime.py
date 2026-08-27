from __future__ import annotations

import asyncio
import json
import hashlib
import logging
import math
import time
from typing import Any

from backend.agent.message import AgentEvent
from backend.tools.base import PermissionLevel
from backend.ws.stream_state import get_stream_content_blocks

APPROVAL_INLINE_PATCH_LIMIT_BYTES = 100_000
APPROVAL_INLINE_FILE_LIMIT = 10
APPROVAL_INLINE_ARGS_LIMIT_BYTES = 200_000
APPROVAL_INLINE_ARG_STRING_LIMIT = 65_536
APPROVAL_ARGUMENT_MAX_DEPTH = 12
APPROVAL_ARGUMENT_MAX_NODES = 4_096
APPROVAL_ARGUMENT_MAX_COLLECTION_ITEMS = 1_024
logger = logging.getLogger(__name__)


class SessionApprovalRuntimeMixin:
    """Mixin providing approval request lifecycle + session-level approval caching."""

    @staticmethod
    def _pending_request_digest(payload: dict[str, Any] | None) -> str:
        source = payload if isinstance(payload, dict) else {}
        request = source.get("request")
        return str(
            source.get("request_digest")
            or (
                request.get("request_digest")
                if isinstance(request, dict)
                else ""
            )
            or ""
        ).strip()

    def _approval_timeout_seconds(self) -> float | None:
        configured = getattr(
            getattr(getattr(self, "config", None), "agent", None),
            "approval_timeout_seconds",
            None,
        )
        try:
            return max(1.0, float(configured)) if configured is not None else None
        except (TypeError, ValueError):
            return None

    def _init_approval_cache(self) -> None:
        """Initialize session approval cache if not already present."""
        if not hasattr(self, "_session_approval_cache"):
            from collections import OrderedDict
            self._session_approval_cache: OrderedDict[str, None] = OrderedDict()
            self._approval_cache_max = 500

    async def _emit_approval_cancelled_once(
        self,
        request_ids: list[str],
        *,
        reason: str,
        conversation_id: str = "",
    ) -> list[str]:
        """Emit at most one terminal notification per approval request."""
        notified = getattr(self, "_settled_approval_notifications", None)
        if not isinstance(notified, set):
            notified = self._settled_approval_notifications = set()
        fresh = [
            request_id
            for request_id in dict.fromkeys(request_ids)
            if request_id and request_id not in notified
        ]
        if not fresh:
            return []

        # A session-wide interrupt can settle approvals belonging to several
        # conversations.  Never collapse those IDs into one unowned event:
        # the frontend would have no safe owner to use during replay and could
        # clear a prompt from the wrong conversation.  Pending approval
        # payloads are the authoritative owner map captured at request time.
        owner = str(conversation_id or "").strip()
        grouped: dict[str, list[str]] = {}
        if owner:
            grouped[owner] = list(fresh)
        else:
            pending_payloads = getattr(self, "_pending_approval_payloads", {})
            for request_id in fresh:
                payload = pending_payloads.get(request_id)
                payload_owner = str((payload or {}).get("conversation_id") or "").strip()
                if not payload_owner:
                    logger.warning(
                        "Skipping unowned approval cancellation: request_id=%s session=%s",
                        request_id,
                        getattr(self, "session_id", ""),
                    )
                    continue
                grouped.setdefault(payload_owner, []).append(request_id)

        emitted: list[str] = []
        for payload_owner, payload_request_ids in grouped.items():
            if not payload_request_ids:
                continue
            notified.update(payload_request_ids)
            event = AgentEvent.approval_cancelled(
                payload_request_ids,
                reason=reason,
                conversation_id=payload_owner,
            )
            try:
                await self._send_event(event)
            except Exception as exc:
                logger.debug("approval cancellation emit failed: %s", exc)
            emitted.extend(payload_request_ids)
        return emitted

    def _approval_cache_key(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        payload: dict[str, Any] | None = None,
    ) -> str:
        """Build a policy-scoped key from the immutable approval request."""
        request_payload = payload or {}
        request_digest = str(
            request_payload.get("request_digest")
            or request_payload.get("request", {}).get("request_digest")
            or ""
        ).strip()
        try:
            args_str = json.dumps(args, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            args_str = str(sorted(args.items())) if isinstance(args, dict) else ""
        session_id = str(getattr(self, "session_id", "") or "").strip()
        if not request_digest:
            canonical = json.dumps(
                {
                    "tool_name": str(tool_name or "").strip(),
                    "arguments": dict(args or {}),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            request_digest = hashlib.sha256(
                canonical.encode("utf-8")
            ).hexdigest()
        conversation_id = str(
            request_payload.get("conversation_id")
            or getattr(self, "active_conversation_id", "")
            or ""
        ).strip()
        workspace_root = str(request_payload.get("workspace_root") or "").strip()
        permission_mode = str(request_payload.get("permission_mode") or "").strip()
        workspace_scope = str(request_payload.get("workspace_scope") or "").strip()
        raw_escalation = args.get("with_escalated_permissions")
        escalation_requested = raw_escalation is True or (
            isinstance(raw_escalation, str)
            and raw_escalation.strip().lower() in {"1", "true", "yes", "on"}
        )
        capability = "escalated" if escalation_requested else "ordinary"
        return (
            f"{session_id}::{conversation_id}::{workspace_root}::{permission_mode}::"
            f"{workspace_scope}::{capability}::{tool_name}::{request_digest}::{args_str}"
        )

    def _is_session_approved(self, tool_name: str, args: dict[str, Any], *, payload: dict[str, Any] | None = None) -> bool:
        """Check if this tool+args was already approved for the session."""
        self._init_approval_cache()
        return self._approval_cache_key(tool_name, args, payload=payload) in self._session_approval_cache

    def _mark_session_approved(self, tool_name: str, args: dict[str, Any], *, payload: dict[str, Any] | None = None) -> None:
        """Remember this tool+args as approved for the session."""
        self._init_approval_cache()
        key = self._approval_cache_key(tool_name, args, payload=payload)
        self._session_approval_cache[key] = None
        self._session_approval_cache.move_to_end(key)
        # Evict oldest if over limit
        while len(self._session_approval_cache) > self._approval_cache_max:
            self._session_approval_cache.popitem(last=False)

    def _resolve_pending_approval(
        self,
        request_id: str,
        payload: dict[str, Any],
    ) -> bool:
        request_key = str(request_id or "").strip()
        if not request_key:
            return False
        pending_payload = self._pending_approval_payloads.get(request_key, {})
        pending_request = pending_payload.get("request") if isinstance(pending_payload, dict) else None
        tool_name = str(
            (pending_request.get("tool_name") if isinstance(pending_request, dict) else "") or ""
        ).strip()
        if tool_name == "exit_plan_mode":
            action = str(payload.get("action") or "").strip().lower()
            # ExitPlanMode is a one-shot human approval. It cannot inherit the
            # generic session cache/remember semantics or be resolved by bulk
            # auto-approval payloads.
            if payload.get("auto_approved") or payload.get("session_approved"):
                return False
            payload = dict(payload)
            payload.pop("remember_for_session", None)
            if action not in {"approve", "reject"}:
                return False
            if action == "approve":
                raw_allowed_prompts = payload.get("command_prompts")
                normalized_prompts: list[str] = []
                if raw_allowed_prompts is not None:
                    if not isinstance(raw_allowed_prompts, list):
                        return False
                    for item in raw_allowed_prompts:
                        if not isinstance(item, dict) or set(item) != {"tool", "prompt"}:
                            return False
                        prompt = str(item.get("prompt") or "").strip()
                        if item.get("tool") != "run_command" or not prompt:
                            return False
                        normalized_prompts.append(prompt)
                if normalized_prompts:
                    payload["command_prompts"] = [
                        {"tool": "run_command", "prompt": prompt}
                        for prompt in normalized_prompts
                    ]
        expected_digest = self._pending_request_digest(pending_payload)
        payload = dict(payload)
        if expected_digest:
            supplied_digest = str(payload.get("request_digest") or "").strip()
            if supplied_digest and supplied_digest != expected_digest:
                return False
            payload["request_digest"] = expected_digest
        self._approval_diff_cache.pop(request_key, None)
        future = self._pending_approvals.pop(request_key, None)
        if future and not future.done():
            future.set_result(payload)
            return True
        # The request is emitted before the waiter is registered. Keep a valid
        # response that races with that hand-off instead of dropping it.
        if request_key in self._pending_approval_payloads:
            queued_responses = getattr(self, "_pending_approval_responses", None)
            if not isinstance(queued_responses, dict):
                queued_responses = {}
                self._pending_approval_responses = queued_responses
            queued_responses[request_key] = payload
            return True
        return False

    def _approval_response_owner_error(
        self,
        request_id: str,
        response: dict[str, Any],
    ) -> str:
        """Validate the response against the pending thread/turn/item owner."""

        request_key = str(request_id or "").strip()
        pending = getattr(self, "_pending_approval_payloads", {}).get(request_key)
        if not isinstance(pending, dict):
            return f"Approval request '{request_key}' is stale or no longer pending"

        expected_conversation_id = str(pending.get("conversation_id") or "").strip()
        supplied_conversation_id = str(
            response.get("conversation_id") or response.get("conversationId") or ""
        ).strip()
        if expected_conversation_id and supplied_conversation_id != expected_conversation_id:
            return "Approval response does not belong to the pending conversation"

        expected_turn_id = str(pending.get("turn_id") or "").strip()
        supplied_turn_id = str(
            response.get("turn_id") or response.get("turnId") or ""
        ).strip()
        if expected_turn_id and supplied_turn_id != expected_turn_id:
            return "Approval response does not belong to the pending turn"
        expected_message_id = str(pending.get("message_id") or "").strip()
        supplied_message_id = str(
            response.get("message_id") or response.get("messageId") or ""
        ).strip()
        if expected_message_id and supplied_message_id != expected_message_id:
            return "Approval response does not belong to the pending message"
        expected_digest = self._pending_request_digest(pending)
        supplied_digest = str(response.get("request_digest") or "").strip()
        if supplied_digest and supplied_digest != expected_digest:
            return "Approval response does not match the pending tool request"
        return ""

    async def _auto_approve_pending_tool_approvals(
        self,
        *,
        reason: str,
        conversation_id: str | None = None,
        only_auto_allowed: bool = False,
    ) -> list[str]:
        pending_payloads = getattr(self, "_pending_approval_payloads", {})
        target_conversation_id = str(conversation_id or "").strip()
        approved_ids: list[str] = []

        for request_id, payload in list(pending_payloads.items()):
            payload_conversation_id = str(payload.get("conversation_id") or "").strip()
            if target_conversation_id and payload_conversation_id != target_conversation_id:
                continue

            request = payload.get("request")
            # Every pending prompt is stored as one ``control_request``; only the
            # tool-approval subtype can be auto-approved.
            if str(payload.get("type") or "").strip() != "control_request":
                continue
            if not isinstance(request, dict):
                continue
            if str(request.get("subtype") or "").strip() != "can_use_tool":
                continue
            tool_name = str(request.get("tool_name") or "").strip()
            if tool_name == "exit_plan_mode":
                continue
            if only_auto_allowed and not self._pending_tool_payload_is_auto_allowed(payload):
                continue

            resolved = self._resolve_pending_approval(
                request_id,
                {
                    "action": "approve",
                    "auto_approved": True,
                    "reason": reason,
                },
            )
            if resolved:
                approved_ids.append(request_id)

        if approved_ids:
            await self._emit_approval_cancelled_once(
                approved_ids,
                reason=reason,
                conversation_id=target_conversation_id,
            )

        return approved_ids

    def _pending_tool_payload_is_auto_allowed(self, payload: dict[str, Any]) -> bool:
        request = payload.get("request")
        if not isinstance(request, dict):
            return False
        tool_name = str(request.get("tool_name") or "").strip()
        args = request.get("input") or {}
        if not tool_name:
            return False
        if not isinstance(args, dict):
            args = {}
        checker = getattr(self, "permission_checker", None)
        context = getattr(self, "permission_context", None)
        if checker is None:
            return False
        registry = getattr(self, "tool_registry", None)
        tool = None
        if registry is not None:
            try:
                tool = registry.get_tool(tool_name)
            except Exception:
                tool = None
        try:
            return checker.check(
                tool_name,
                args,
                context=context,
                tool=tool,
            ) == PermissionLevel.AUTO
        except Exception as exc:
            logger.debug("pending approval auto-check failed for %s: %s", tool_name, exc)
            return False

    def _normalize_control_response(
        self,
        data: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        request_id = str(data.get("request_id") or "").strip()
        response = data.get("response")
        if not isinstance(response, dict):
            payload = dict(data)
            payload.pop("request_id", None)
            payload.pop("requestId", None)
            payload.pop("response", None)
            return request_id, payload

        request_id = str(response.get("request_id") or request_id).strip()
        subtype = str(response.get("subtype", "success")).strip().lower()
        payload_raw = response.get("response")
        payload = dict(payload_raw) if isinstance(payload_raw, dict) else {}

        if subtype == "error":
            guidance = str(response.get("error") or "control response rejected").strip()
            return request_id, {"action": "reject", "guidance": guidance}

        action_raw = payload.get("action")
        if isinstance(action_raw, bool):
            payload["action"] = "approve" if action_raw else "reject"
        action = str(payload.get("action") or "").strip().lower()
        if action in {"accept", "approve", "allow", "yes"}:
            payload["action"] = "approve"
        elif action in {"decline", "deny", "reject", "cancel", "no"}:
            payload["action"] = "reject"

        content = payload.get("content")
        if "answer" not in payload and isinstance(content, str) and content.strip():
            payload["answer"] = content.strip()

        message = payload.get("message")
        if "guidance" not in payload and isinstance(message, str) and message.strip():
            payload["guidance"] = message.strip()

        # Tab-to-amend: user-supplied feedback on approve/reject becomes the
        # model-facing guidance (mirrors cc's PermissionPrompt feedback).
        feedback = payload.get("feedback")
        if "guidance" not in payload and isinstance(feedback, str) and feedback.strip():
            payload["guidance"] = feedback.strip()

        for key in ("conversation_id", "conversationId", "turn_id", "turnId", "message_id"):
            value = data.get(key)
            if value not in (None, ""):
                payload.setdefault(key, value)

        return request_id, payload

    async def _approval_handler(self, tool_call_id: str) -> dict[str, Any]:
        # Check session cache first
        payload = self._pending_approval_payloads.get(tool_call_id, {})
        request = payload.get("request")
        request = request if isinstance(request, dict) else {}
        tool_name = str(request.get("tool_name") or "").strip()
        args = request.get("input") or {}
        if tool_name != "exit_plan_mode" and tool_name and self._is_session_approved(tool_name, args, payload=payload):
            logger.debug("Session-approved: %s %s", tool_name, tool_call_id)
            return {
                "action": "approve",
                "session_approved": True,
                "request_digest": self._pending_request_digest(payload),
            }

        queued_response = getattr(self, "_pending_approval_responses", {}).pop(tool_call_id, None)
        if queued_response is not None:
            return queued_response

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_approvals[tool_call_id] = future
        # The request payload is emitted before this waiter registers, so a
        # fast client response can land in the hand-off queue between the
        # entry check above and the registration here. Re-check once after
        # registering; otherwise the queued response is never consumed.
        queued_after_register = getattr(self, "_pending_approval_responses", {}).pop(tool_call_id, None)
        if queued_after_register is not None:
            future.set_result(queued_after_register)
        try:
            timeout_seconds = self._approval_timeout_seconds()
            result = await asyncio.wait_for(future, timeout=timeout_seconds)
            # Remember approval for session if user opted in
            if isinstance(result, dict) and result.get("action") == "approve":
                if result.get("remember_for_session") and tool_name and tool_name != "exit_plan_mode":
                    self._mark_session_approved(tool_name, args, payload=payload)
            return result
        except asyncio.TimeoutError:
            if not future.done():
                future.cancel()
            conversation_id = str(payload.get("conversation_id") or "").strip()
            await self._emit_approval_cancelled_once(
                [tool_call_id],
                reason="approval_timeout",
                conversation_id=conversation_id,
            )
            return {
                "action": "reject",
                "guidance": f"approval timed out after {timeout_seconds:g} seconds",
            }
        except asyncio.CancelledError:
            if not future.done():
                future.cancel()
            await self._emit_approval_cancelled_once(
                [tool_call_id],
                reason="approval_wait_cancelled",
                conversation_id=str(payload.get("conversation_id") or "").strip(),
            )
            raise
        finally:
            self._pending_approvals.pop(tool_call_id, None)
            self._pending_approval_payloads.pop(tool_call_id, None)
            getattr(self, "_pending_approval_responses", {}).pop(tool_call_id, None)
            self._approval_diff_cache.pop(tool_call_id, None)

    async def _cancel_pending_approvals(
        self,
        *,
        reason: str = "run_cancelled",
        conversation_id: str | None = None,
    ) -> list[str]:
        pending_payloads = getattr(self, "_pending_approval_payloads", {})
        target_conversation_id = str(conversation_id or "").strip()
        if target_conversation_id:
            request_ids = [
                request_id for request_id, payload in pending_payloads.items()
                if str(payload.get("conversation_id") or "").strip() == target_conversation_id
            ]
        else:
            request_ids = list(dict.fromkeys([
                *getattr(self, "_pending_approvals", {}).keys(),
                *pending_payloads.keys(),
                *getattr(self, "_pending_approval_responses", {}).keys(),
            ]))
        for request_id in request_ids:
            future = getattr(self, "_pending_approvals", {}).get(request_id)
            if future and not future.done():
                future.cancel()
        try:
            if request_ids:
                # The pending payload is the authoritative request ->
                # conversation owner map.  Emit while that map still exists;
                # clearing it first turns valid owned cancellations into
                # "unowned" events and leaves the frontend prompt stuck.
                await self._emit_approval_cancelled_once(
                    request_ids,
                    reason=reason,
                    conversation_id=target_conversation_id,
                )
        finally:
            # Cancellation is terminal even if the socket disappears while
            # the notification is being sent.  Always release waiters and all
            # associated payload/diff state.
            for request_id in request_ids:
                getattr(self, "_pending_approvals", {}).pop(request_id, None)
                self._pending_approval_payloads.pop(request_id, None)
                getattr(self, "_pending_approval_responses", {}).pop(request_id, None)
                self._approval_diff_cache.pop(request_id, None)
        return request_ids

    async def _reject_pending_approvals(
        self,
        *,
        reason: str,
        guidance: str,
        conversation_id: str | None = None,
    ) -> list[str]:
        """Resolve approval waits without cancelling the owning agent task.

        A user steer supersedes the action that is waiting for approval, but it
        must not propagate ``CancelledError`` through the tool batch and abort
        the entire run. Resolve the waiters as explicit rejections so the
        current batch can finish and the turn-local input is consumed at the
        normal tool boundary.
        """
        pending_payloads = getattr(self, "_pending_approval_payloads", {})
        target_conversation_id = str(conversation_id or "").strip()
        request_ids: list[str] = []
        for request_id, payload in list(pending_payloads.items()):
            payload_conversation_id = str(payload.get("conversation_id") or "").strip()
            if target_conversation_id and payload_conversation_id != target_conversation_id:
                continue
            if self._resolve_pending_approval(
                request_id,
                {
                    "action": "reject",
                    "guidance": guidance,
                    "reason": reason,
                    "superseded": True,
                },
            ):
                request_ids.append(request_id)

        if request_ids:
            await self._emit_approval_cancelled_once(
                request_ids,
                reason=reason,
                conversation_id=target_conversation_id,
            )
        return request_ids

    async def _reemit_pending_state(
        self,
        conversation_id: str | None = None,
        *,
        skip_stream_conversation_ids: set[str] | None = None,
    ) -> None:
        target_conversation_id = str(conversation_id or "").strip()
        skip_stream_conversation_ids = skip_stream_conversation_ids or set()
        for payload in list(self._pending_approval_payloads.values()):
            if target_conversation_id:
                payload_conversation_id = str(payload.get("conversation_id") or "").strip()
                if payload_conversation_id != target_conversation_id:
                    continue
            try:
                await self._send_ws_payload(payload, log_context="reemit:approval")
            except Exception as exc:
                logger.debug("reemit approval failed: %s", exc)

        emitted_conversations: set[str] = set()
        stream_states = getattr(self, "_conversation_streams", {})
        for stream_conversation_id, task in list(getattr(self, "_conversation_run_tasks", {}).items()):
            if not stream_conversation_id or task is None or task.done():
                continue
            if stream_conversation_id in skip_stream_conversation_ids:
                continue
            if target_conversation_id and stream_conversation_id != target_conversation_id:
                continue
            stream_state = stream_states.get(stream_conversation_id)
            if not stream_state:
                continue
            tool_calls = stream_state.get("tool_calls")
            all_tool_states = list(tool_calls.values()) if isinstance(tool_calls, dict) else []
            pending_tool_calls = [
                item
                for item in all_tool_states
                if str(item.get("status") or "running").lower()
                not in {"success", "completed", "failed", "error", "blocked", "cancelled", "timeout"}
            ]
            await self._send_event(
                AgentEvent.stream_resume(
                    stream_conversation_id,
                    stream_state.get("message_id") or None,
                    pending_tool_calls,
                    get_stream_content_blocks(stream_state),
                    turn_id=str(stream_state.get("turn_id") or ""),
                    phase=str(stream_state.get("phase") or ""),
                    stream_status=str(stream_state.get("status") or "running"),
                    event_seq=int(stream_state.get("event_seq") or 0),
                    last_event_type=str(stream_state.get("last_event_type") or ""),
                    tool_states=all_tool_states,
                )
            )
            emitted_conversations.add(stream_conversation_id)

        # Legacy fallback removed: only per-conversation stream state is authoritative

    def _clone_json_dict(self, payload: dict[str, Any]) -> dict[str, Any]:
        return json.loads(json.dumps(payload))

    @staticmethod
    def _approval_arg_summary(value: Any) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "projection_omitted": True,
            "kind": type(value).__name__,
        }
        if isinstance(value, str):
            summary["characters"] = len(value)
        elif isinstance(value, (list, tuple)):
            summary["items"] = len(value)
        elif isinstance(value, dict):
            summary["fields"] = len(value)
        return summary

    def _sanitize_approval_args_for_client(self, args: dict[str, Any]) -> dict[str, Any]:
        """Keep approval evidence useful without shipping unbounded tool input."""

        cloned_args = self._clone_json_dict(args)
        nodes = 0
        invalid_field_count = 0

        def project(value: Any, depth: int) -> Any:
            nonlocal nodes, invalid_field_count
            nodes += 1
            if nodes > APPROVAL_ARGUMENT_MAX_NODES:
                return {
                    "projection_omitted": True,
                    "kind": "node_budget",
                }
            if depth > APPROVAL_ARGUMENT_MAX_DEPTH:
                return {
                    "projection_omitted": True,
                    "kind": "depth",
                    "depth": depth,
                }
            if isinstance(value, str):
                if len(value) <= APPROVAL_INLINE_ARG_STRING_LIMIT:
                    return value
                marker = (
                    f"\n[... {len(value) - APPROVAL_INLINE_ARG_STRING_LIMIT} "
                    "characters omitted from approval projection ...]\n"
                )
                available = max(0, APPROVAL_INLINE_ARG_STRING_LIMIT - len(marker))
                head = available // 2
                tail = available - head
                return value[:head] + marker + (value[-tail:] if tail else "")
            if value is None or isinstance(value, (bool, int)):
                return value
            if isinstance(value, float):
                return value if math.isfinite(value) else self._approval_arg_summary(value)
            if isinstance(value, list):
                limit = min(
                    len(value),
                    APPROVAL_ARGUMENT_MAX_COLLECTION_ITEMS,
                    max(0, APPROVAL_ARGUMENT_MAX_NODES - nodes),
                )
                projected_items = [project(item, depth + 1) for item in value[:limit]]
                if limit < len(value):
                    projected_items.append({
                        "projection_omitted": True,
                        "kind": "items",
                        "omitted_items": len(value) - limit,
                    })
                return projected_items
            if isinstance(value, dict):
                projected_fields: dict[str, Any] = {}
                omitted_fields = 0
                for key, item in value.items():
                    if (
                        len(projected_fields) >= APPROVAL_ARGUMENT_MAX_COLLECTION_ITEMS - 1
                        or nodes >= APPROVAL_ARGUMENT_MAX_NODES
                    ):
                        omitted_fields += 1
                        continue
                    if not isinstance(key, str) or not key.strip() or len(key) > 1_024:
                        invalid_field_count += 1
                        omitted_fields += 1
                        continue
                    projected_fields[key] = project(item, depth + 1)
                if omitted_fields:
                    marker_key = "_projection" if "_projection" not in projected_fields else "_minicode_projection"
                    projected_fields[marker_key] = {
                        "truncated": True,
                        "omitted_field_count": omitted_fields,
                    }
                return projected_fields
            return self._approval_arg_summary(value)

        projected_args = project(cloned_args, 0)
        client_args = projected_args if isinstance(projected_args, dict) else {
            "value": projected_args,
        }

        if invalid_field_count:
            marker = client_args.get("_projection")
            if not isinstance(marker, dict):
                marker = {"truncated": True}
                client_args["_projection"] = marker
            marker["invalid_field_count"] = invalid_field_count

        encoded = json.dumps(client_args, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) <= APPROVAL_INLINE_ARGS_LIMIT_BYTES:
            return client_args

        projected: dict[str, Any] = {}
        used = 2
        preferred = (
            "command",
            "cwd",
            "path",
            "file_path",
            "url",
            "query",
            "pattern",
            "name",
        )
        ordered_keys = [key for key in preferred if key in client_args]
        ordered_keys.extend(key for key in client_args if key not in ordered_keys)
        omitted_fields: list[str] = []
        omitted_field_count = 0
        for index, key in enumerate(ordered_keys):
            if index >= 1_023:
                omitted_field_count += len(ordered_keys) - index
                omitted_fields.extend(ordered_keys[index : index + 256])
                break
            value = client_args[key]
            candidate = json.dumps({key: value}, ensure_ascii=False, separators=(",", ":"))
            cost = max(0, len(candidate) - 2) + (1 if projected else 0)
            if used + cost <= APPROVAL_INLINE_ARGS_LIMIT_BYTES:
                projected[key] = value
                used += cost
                continue
            projected[key] = self._approval_arg_summary(args.get(key))
            omitted_fields.append(key)
            omitted_field_count += 1
        projected["_projection"] = {
            "truncated": True,
            "omitted_fields": omitted_fields[:256],
            "omitted_field_count": omitted_field_count,
            "invalid_field_count": invalid_field_count,
            "original_characters": len(encoded),
        }
        return projected

    def _sanitize_approval_diff_for_client(
        self,
        tool_call_id: str,
        diff: Any,
        *,
        conversation_id: str = "",
        turn_id: str = "",
        workspace_root: str = "",
    ) -> Any:
        if not isinstance(diff, dict) or diff.get("format") != "structured":
            return diff

        files = diff.get("files")
        if not isinstance(files, list):
            return diff

        cached_diff = self._clone_json_dict(diff)
        cached_diff["_owner"] = {
            "conversation_id": str(conversation_id or "").strip(),
            "turn_id": str(turn_id or "").strip(),
            "workspace_root": str(workspace_root or "").strip(),
        }
        self._approval_diff_cache[tool_call_id] = cached_diff
        total_patch_bytes = sum(
            len(item.get("patch", ""))
            for item in files
            if isinstance(item, dict) and isinstance(item.get("patch"), str)
        )
        should_defer_patch = (
            len(files) > APPROVAL_INLINE_FILE_LIMIT
            or total_patch_bytes > APPROVAL_INLINE_PATCH_LIMIT_BYTES
        )
        client_diff = self._clone_json_dict(diff)
        if not should_defer_patch:
            return client_diff

        for item in client_diff.get("files", []):
            if isinstance(item, dict) and isinstance(item.get("patch"), str) and item.get("patch"):
                item["patch"] = None
                item["is_large"] = True

        return client_diff

    def _build_approval_request_payload(self, event: AgentEvent) -> dict[str, Any]:
        request_id = str(event.data.get("tool_call_id", "")).strip()
        conversation_id = str(event.data.get("conversation_id") or "").strip()
        # Scratch space for the fields the control request is assembled from;
        # the wire payload is the ``control_request`` built at the end.
        payload: dict[str, Any] = {
            "tool_name": str(event.data.get("tool_name", "")).strip(),
            "args": self._sanitize_approval_args_for_client(
                dict(event.data.get("args") or {})
            ),
        }
        request_digest = str(event.data.get("request_digest") or "").strip()
        if request_digest:
            payload["request_digest"] = request_digest
        timeout_seconds = self._approval_timeout_seconds()
        if timeout_seconds is not None:
            payload["timeout_seconds"] = timeout_seconds
            payload["expires_at"] = int(time.time() * 1000 + timeout_seconds * 1000)
        for key in ("source_agent", "source_thread", "source_tool"):
            value = str(event.data.get(key) or "").strip()
            if value:
                payload[key] = value
        if conversation_id:
            payload["conversation_id"] = conversation_id
        for key in ("turn_id", "message_id"):
            value = str(event.data.get(key) or "").strip()
            if value:
                payload[key] = value
        conversation = None
        repository = getattr(self, "conversation_repo", None)
        if conversation_id and repository is not None:
            try:
                conversation = repository.get_conversation(conversation_id)
            except Exception:
                conversation = None
        workspace_root = str(
            getattr(conversation, "worktree_path", "")
            or getattr(conversation, "workspace_root", "")
            or ""
        ).strip()
        if workspace_root:
            payload["workspace_root"] = workspace_root
        permission_mode = str(
            getattr(conversation, "permission_mode", "")
            or getattr(getattr(self, "permission_context", None), "mode", "")
        ).strip()
        if permission_mode:
            payload["permission_mode"] = permission_mode
        workspace_scope = str(
            getattr(getattr(self, "permission_context", None), "workspace_scope", "")
        ).strip()
        if workspace_scope:
            payload["workspace_scope"] = workspace_scope
        diff = self._sanitize_approval_diff_for_client(
            request_id,
            event.data.get("diff"),
            conversation_id=conversation_id,
            turn_id=str(event.data.get("turn_id") or ""),
            workspace_root=workspace_root,
        )
        if isinstance(diff, str) and diff.strip():
            payload["diff"] = diff
        elif isinstance(diff, dict) and diff:
            payload["diff"] = diff

        request: dict[str, Any] = {
            "subtype": "can_use_tool",
            "tool_name": payload["tool_name"],
            "input": payload["args"],
            "tool_use_id": request_id,
        }
        if request_digest:
            request["request_digest"] = request_digest
        if "diff" in payload:
            request["diff"] = payload["diff"]
        for key in ("source_agent", "source_thread", "source_tool"):
            if key in payload:
                request[key] = payload[key]
        control_payload = {
            "type": "control_request",
            "request_id": request_id,
            "request": request,
        }
        for key in ("timeout_seconds", "expires_at"):
            if key in payload:
                control_payload[key] = payload[key]
        if conversation_id:
            control_payload["conversation_id"] = conversation_id
        for key in ("turn_id", "message_id", "workspace_root", "permission_mode", "workspace_scope"):
            if key in payload:
                control_payload[key] = payload[key]
        self._pending_approval_payloads[request_id] = control_payload
        return control_payload
