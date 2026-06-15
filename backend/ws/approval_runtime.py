from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from backend.agent.message import AgentEvent

APPROVAL_INLINE_PATCH_LIMIT_BYTES = 100_000
APPROVAL_INLINE_FILE_LIMIT = 10
logger = logging.getLogger(__name__)


class SessionApprovalRuntimeMixin:
    """Mixin providing approval request lifecycle + session-level approval caching."""

    def _init_approval_cache(self) -> None:
        """Initialize session approval cache if not already present."""
        if not hasattr(self, "_session_approval_cache"):
            from collections import OrderedDict
            self._session_approval_cache: OrderedDict[str, None] = OrderedDict()
            self._approval_cache_max = 500

    def _approval_cache_key(self, tool_name: str, args: dict[str, Any]) -> str:
        """Build a stable cache key from tool name + sorted args."""
        try:
            args_str = json.dumps(args, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            args_str = str(sorted(args.items())) if isinstance(args, dict) else ""
        return f"{tool_name}::{args_str}"

    def _is_session_approved(self, tool_name: str, args: dict[str, Any]) -> bool:
        """Check if this tool+args was already approved for the session."""
        self._init_approval_cache()
        return self._approval_cache_key(tool_name, args) in self._session_approval_cache

    def _mark_session_approved(self, tool_name: str, args: dict[str, Any]) -> None:
        """Remember this tool+args as approved for the session."""
        self._init_approval_cache()
        key = self._approval_cache_key(tool_name, args)
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
        self._approval_diff_cache.pop(request_key, None)
        future = self._pending_approvals.pop(request_key, None)
        if future and not future.done():
            future.set_result(payload)
            return True
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

        return request_id, payload

    async def _approval_handler(self, tool_call_id: str) -> dict[str, Any]:
        # Check session cache first
        payload = self._pending_approval_payloads.get(tool_call_id, {})
        tool_name = str(payload.get("tool_name") or payload.get("request", {}).get("tool_name") or "").strip()
        args = payload.get("args") or payload.get("request", {}).get("input") or {}
        if tool_name and self._is_session_approved(tool_name, args):
            logger.debug("Session-approved: %s %s", tool_name, tool_call_id)
            return {"action": "approve", "session_approved": True}

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_approvals[tool_call_id] = future
        try:
            result = await asyncio.wait_for(future, timeout=300)
            # Remember approval for session if user opted in
            if isinstance(result, dict) and result.get("action") == "approve":
                if result.get("remember_for_session") and tool_name:
                    self._mark_session_approved(tool_name, args)
            return result
        except asyncio.TimeoutError:
            if not future.done():
                future.cancel()
            event = AgentEvent.approval_cancelled([tool_call_id], reason="approval_timeout")
            conversation_id = str(payload.get("conversation_id") or "").strip()
            if conversation_id:
                event.data["conversation_id"] = conversation_id
            try:
                await self._send_event(event)
            except Exception as exc:
                logger.debug("approval timeout cancellation emit failed: %s", exc)
            return {"action": "reject", "guidance": "approval timed out after 5 minutes"}
        except asyncio.CancelledError:
            if not future.done():
                future.cancel()
            raise
        finally:
            self._pending_approvals.pop(tool_call_id, None)
            self._pending_approval_payloads.pop(tool_call_id, None)
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
            ]))
        for request_id in request_ids:
            future = getattr(self, "_pending_approvals", {}).get(request_id)
            if future and not future.done():
                future.cancel()
        for request_id in request_ids:
            getattr(self, "_pending_approvals", {}).pop(request_id, None)
        for request_id in request_ids:
            self._pending_approval_payloads.pop(request_id, None)
            self._approval_diff_cache.pop(request_id, None)
        if request_ids:
            event = AgentEvent.approval_cancelled(request_ids, reason=reason)
            if target_conversation_id:
                event.data["conversation_id"] = target_conversation_id
            await self._send_event(event)
        return request_ids

    async def _reemit_pending_state(self, conversation_id: str | None = None) -> None:
        target_conversation_id = str(conversation_id or "").strip()
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
            if target_conversation_id and stream_conversation_id != target_conversation_id:
                continue
            stream_state = stream_states.get(stream_conversation_id)
            if not stream_state:
                continue
            tool_calls = stream_state.get("tool_calls")
            await self._send_ws_payload(
                {
                    "type": "stream_resume",
                    "conversation_id": stream_conversation_id,
                    "message_id": stream_state.get("message_id") or "",
                    "accumulated_text": stream_state.get("accumulated_text") or "",
                    "tool_calls_pending": list(tool_calls.values()) if isinstance(tool_calls, dict) else [],
                },
                log_context="reemit:stream_resume",
            )
            emitted_conversations.add(stream_conversation_id)

        # Legacy fallback removed: only per-conversation stream state is authoritative

    def _clone_json_dict(self, payload: dict[str, Any]) -> dict[str, Any]:
        return json.loads(json.dumps(payload))

    def _sanitize_approval_diff_for_client(
        self,
        tool_call_id: str,
        diff: Any,
    ) -> Any:
        if not isinstance(diff, dict) or diff.get("format") != "structured":
            return diff

        files = diff.get("files")
        if not isinstance(files, list):
            return diff

        self._approval_diff_cache[tool_call_id] = self._clone_json_dict(diff)
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
        payload: dict[str, Any] = {
            "type": "approval_request",
            "tool_call_id": request_id,
            "tool_name": str(event.data.get("tool_name", "")).strip(),
            "args": dict(event.data.get("args") or {}),
        }
        if conversation_id:
            payload["conversation_id"] = conversation_id
        diff = self._sanitize_approval_diff_for_client(request_id, event.data.get("diff"))
        if isinstance(diff, str) and diff.strip():
            payload["diff"] = diff
        elif isinstance(diff, dict) and diff:
            payload["diff"] = diff

        if not self._use_control_protocol:
            self._pending_approval_payloads[request_id] = payload
            return payload

        request: dict[str, Any] = {
            "subtype": "can_use_tool",
            "tool_name": payload["tool_name"],
            "input": payload["args"],
            "tool_use_id": request_id,
        }
        if "diff" in payload:
            request["diff"] = payload["diff"]
        control_payload = {
            "type": "control_request",
            "request_id": request_id,
            "request": request,
        }
        if conversation_id:
            control_payload["conversation_id"] = conversation_id
        self._pending_approval_payloads[request_id] = control_payload
        return control_payload
