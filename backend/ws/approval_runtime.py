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

        action = str(payload.get("action") or "").strip().lower()
        if action in {"accept", "approve", "allow", "yes"}:
            payload["action"] = "approve"
        elif action in {"decline", "deny", "reject", "cancel"}:
            payload["action"] = "reject"

        content = payload.get("content")
        if "answer" not in payload and isinstance(content, str) and content.strip():
            payload["answer"] = content.strip()

        message = payload.get("message")
        if "guidance" not in payload and isinstance(message, str) and message.strip():
            payload["guidance"] = message.strip()

        return request_id, payload

    async def _approval_handler(self, tool_call_id: str) -> dict[str, Any]:
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_approvals[tool_call_id] = future
        try:
            return await asyncio.wait_for(future, timeout=300)
        except asyncio.TimeoutError:
            return {"action": "reject", "guidance": "approval timed out after 5 minutes"}
        except asyncio.CancelledError:
            if not future.done():
                future.cancel()
            raise
        finally:
            self._pending_approvals.pop(tool_call_id, None)
            self._pending_approval_payloads.pop(tool_call_id, None)
            self._approval_diff_cache.pop(tool_call_id, None)

    async def _cancel_pending_approvals(self, *, reason: str = "run_cancelled") -> list[str]:
        request_ids = list(getattr(self, "_pending_approvals", {}).keys())
        for future in list(getattr(self, "_pending_approvals", {}).values()):
            if future and not future.done():
                future.cancel()
        getattr(self, "_pending_approvals", {}).clear()
        for request_id in request_ids:
            self._pending_approval_payloads.pop(request_id, None)
            self._approval_diff_cache.pop(request_id, None)
        if request_ids:
            await self._send_event(AgentEvent.approval_cancelled(request_ids, reason=reason))
        return request_ids

    async def _reemit_pending_state(self) -> None:
        for payload in list(self._pending_approval_payloads.values()):
            try:
                await self._send_ws_payload(payload, log_context="reemit:approval")
            except Exception as exc:
                logger.debug("reemit approval failed: %s", exc)

        if self._active_run_task and not self._active_run_task.done():
            await self._send_ws_payload(
                {
                    "type": "stream_resume",
                    "conversation_id": self._streaming_conversation_id or self.active_conversation_id or "",
                    "message_id": self._streaming_message_id,
                    "accumulated_text": self._streaming_accumulated_text,
                    "tool_calls_pending": [],
                },
                log_context="reemit:stream_resume",
            )

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
        payload: dict[str, Any] = {
            "type": "approval_request",
            "tool_call_id": request_id,
            "tool_name": str(event.data.get("tool_name", "")).strip(),
            "args": dict(event.data.get("args") or {}),
        }
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
        self._pending_approval_payloads[request_id] = control_payload
        return control_payload
