"""Source allowlist-miss review, owned by the original process and thread.

Only the native policy decider calls this consumer. Domain matching and hard
denials remain in Source; an approval resumes its waiting proxy request.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any, Callable
from uuid import uuid4

from backend.agent.final_tool_request import canonical_tool_request_digest
from backend.agent.guardian.action import GuardianAction
from backend.permissions.approval_policy import ApprovalPolicy
from backend.permissions.grants import current_granted_permissions, permission_profile_payload
from backend.workspace.executor_paths import ExecutorPath


_PROTOCOLS = {"http": "http", "https_connect": "https", "socks5_tcp": "socks5-tcp", "socks5_udp": "socks5-udp"}
_WIRE_PROTOCOLS = {"http": "http", "https": "https", "socks5-tcp": "socks5_tcp", "socks5-udp": "socks5_udp"}
_ASCII_FOLD = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_DENY = {"type": "deny", "reason": "not_allowed"}
_ABANDONED = "network approval was cancelled before a decision was returned"


@dataclass(frozen=True)
class HostApprovalKey:
    environment_id: str
    host: str
    protocol: str
    port: int

    @classmethod
    def capture(cls, request):
        return cls(request["environmentId"], request["host"].translate(_ASCII_FOLD),
            _PROTOCOLS[request["protocol"]], request["port"])

    @property
    def approval_id(self):
        return f"network#{self.environment_id}#{self.protocol}#{self.host}#{self.port}"


class NetworkApprovalService:
    def __init__(self, *, conversation_id=""):
        self.conversation_id = conversation_id
        self._review_source: Callable[[], Any] = lambda: None
        self._registrations: dict[str, NetworkApprovalRegistration] = {}
        self._pending: dict[tuple[HostApprovalKey, str, str], asyncio.Task] = {}
        self._session_approved: set[HostApprovalKey] = set()
        self._session_denied: set[HostApprovalKey] = set()
        self._policy_commit = asyncio.Lock()

    def bind_review_source(self, getter):
        self._review_source = getter

    def register(self, final_request, context, *, execution_id, cancel_execution, persist_amendment=None):
        registration = NetworkApprovalRegistration(self, final_request, context, execution_id,
            cancel_execution, persist_amendment)
        self._registrations[execution_id] = registration
        return registration


class NetworkApprovalRegistration:
    def __init__(self, service, request, context, execution_id, cancel_execution, persist_amendment):
        self.service = service
        self.request = request
        self.execution_id = execution_id
        self.cancel_execution = cancel_execution
        self.persist_amendment = persist_amendment
        self.command = request.exec_policy.command
        self.environment_id = self.command.environment_id
        run = context.run_context
        self.snapshot = run.turn_environments.clone() if run is not None and run.turn_environments is not None else None
        captured_run = replace(run, turn_environments=self.snapshot) if run is not None else None
        self.context = replace(context, metadata=dict(context.metadata), run_context=captured_run)
        self.cwd = self.command.cwd.native if isinstance(self.command.cwd, ExecutorPath) else str(self.command.cwd)
        self.trigger = {"callId": request.tool_call_id, "toolName": request.tool_name,
            "command": list(self.command.argv), "cwd": self.cwd,
            "sandboxPermissions": self.command.sandbox_permissions, "tty": bool(request.arguments.get("tty", False))}
        additional = current_granted_permissions(context)
        if not additional.is_empty:
            self.trigger["additionalPermissions"] = permission_profile_payload(additional)
        if request.arguments.get("justification") is not None:
            self.trigger["justification"] = request.arguments["justification"]
        self._tasks: set[asyncio.Task] = set()
        self._review_waiters: dict[asyncio.Task, asyncio.Task] = {}
        self._resolved_reviews: set[asyncio.Task] = set()
        self._finish_task: asyncio.Task | None = None
        self._finish_reason: str | None = None
        self._cancel_requested = False
        self.outcome: str | None = None

    def record_outcome(self, message, *, explicit=False):
        # An explicit rejection wins over a proxy-disconnect/blocked fallback.
        if explicit or self.outcome is None:
            self.outcome = message

    async def _cancel(self):
        if not self._cancel_requested and self._finish_reason != "process_finished":
            self._cancel_requested = True
            await self.cancel_execution(self.outcome or _ABANDONED)

    async def decide(self, raw_request):
        if (raw_request["executionId"] != self.execution_id
                or raw_request["environmentId"] != self.environment_id
                or self._finish_reason is not None or self._cancel_requested):
            return dict(_DENY)
        key = HostApprovalKey.capture(raw_request)
        async with self.service._policy_commit:
            if key in self.service._session_denied:
                return dict(_DENY)
            if key in self.service._session_approved:
                return {"type": "allow"}
        active = self.service._review_source()
        target = f"{key.protocol}://{raw_request['host']}:{key.port}"
        blocked = f'Network access to "{target}" was blocked by policy.'
        if active is None:
            self.record_outcome(blocked, explicit=True)
            await self._cancel()
            return dict(_DENY)
        pending_key = key, self.context.turn_id, self.execution_id
        task = self.service._pending.get(pending_key)
        if task is None:
            task = asyncio.create_task(self._resolve(key, raw_request["host"], target, blocked, active),
                name=f"network-approval:{self.execution_id}:{key.approval_id}")
            self.service._pending[pending_key] = task
            self._tasks.add(task)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if self._review_ready(task):
                return await asyncio.shield(task)
            if not task.done():
                self.record_outcome(_ABANDONED)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await self._cancel()
            if self._finish_reason is not None or task.cancelled():
                return dict(_DENY)
            raise
        finally:
            if task.done():
                self._tasks.discard(task)
                self._resolved_reviews.discard(task)
                if self.service._pending.get(pending_key) is task:
                    del self.service._pending[pending_key]

    def _review_context(self, active, approval_id, digest):
        original = self.context
        run = active.run_context
        if run is not None and original.run_context is not None:
            source = original.run_context
            run = replace(run, turn_environments=self.snapshot,
                environment_manager=source.environment_manager, environment_manager_lease=source.environment_manager_lease,
                user_shell=dict(source.user_shell), environment_host_mcp_manager=source.environment_host_mcp_manager,
                environment_host_skill_manager=source.environment_host_skill_manager,
                environment_host_captured=source.environment_host_captured)
        permission = replace(active.permission, environment_scope=original.permission.environment_scope,
            additional_permissions=original.permission.additional_permissions,
            captured_exec_policy=original.permission.captured_exec_policy,
            strict_auto_review=(active.permission.strict_auto_review
                or active.permission_grants.strict_auto_review_enabled(active.turn_id)))
        return replace(active, run_context=run, permission=permission, environment_id=self.environment_id,
            turn_environment=original.turn_environment, environment_bindings=None,
            workspace_root=original.workspace_root, sandbox_policy=original.sandbox_policy,
            tool_call_id=approval_id, authorization_digest=digest,
            authorization_policy_identity="network_access", final_tool_request=None)

    async def _emit(self, context, kind, data):
        if context.emit_event is not None:
            await context.emit_event(kind, {**data, "conversation_id": context.conversation_id,
                "turn_id": context.turn_id, "message_id": context.metadata.get("assistant_message_id", "")})

    async def _resolve(self, key, host, target, blocked, active):
        prompt_id = f"{key.approval_id}#{uuid4().hex}"
        digest = canonical_tool_request_digest("network_access", {"execution_digest": self.request.digest,
            "execution_id": self.execution_id, "environment_id": self.environment_id,
            "host": host, "protocol": key.protocol, "port": key.port})
        review = self._review_context(active, prompt_id, digest)
        try:
            if not self.command.managed_profile or not ApprovalPolicy.from_context(review.permission).permits("network_access"):
                self.record_outcome(blocked, explicit=True)
                await self._cancel()
                return dict(_DENY)
            reason = f"{host} is not in the allowed_domains"
            response = await self._wait_for_review(self._request_review(review, key, host, target, reason, blocked))
            if response.get("request_digest", digest) != digest:
                self.record_outcome("The approval response did not match the exact network request.", explicit=True)
                await self._cancel()
                return dict(_DENY)
            decision = response.get("decision", "approved" if response.get("action") == "approve" else "abort")
            async with self.service._policy_commit:
                if decision in ("approved", "approved_for_session"):
                    if key in self.service._session_denied:
                        self.record_outcome(blocked, explicit=True)
                    else:
                        if decision == "approved_for_session":
                            self.service._session_approved.add(key)
                        return {"type": "allow"}
                elif isinstance(decision, dict) and "network_policy_amendment" in decision:
                    amendment = decision["network_policy_amendment"]["network_policy_amendment"]
                    action = amendment["action"]
                    if action not in {"allow", "deny"}:
                        raise ValueError("The network policy amendment action must be allow or deny")
                    applied = False

                    def on_policy_applied():
                        nonlocal applied
                        applied = True
                        if action == "allow":
                            self.service._session_denied.discard(key)
                            self.service._session_approved.add(key)

                    if action == "deny":
                        self.record_outcome("rejected by user", explicit=True)
                    try:
                        if self.persist_amendment is None:
                            raise RuntimeError("The issuing execution has no network policy amendment writer")
                        await self.persist_amendment(host=amendment["host"], approved_host=host,
                            protocol=_WIRE_PROTOCOLS[key.protocol], action=action, reason=reason,
                            on_policy_applied=on_policy_applied)
                    except Exception as error:
                        await self._emit(review, "system_notice", {"level": "warning",
                            "message": f"Failed to apply network policy amendment: {error}"})
                    if action == "deny":
                        self.service._session_approved.discard(key)
                        self.service._session_denied.add(key)
                    elif applied:
                        self.service._session_denied.discard(key)
                        self.service._session_approved.add(key)
                        return {"type": "allow"}
                    else:
                        self.record_outcome(blocked, explicit=True)
                else:
                    self.record_outcome(response.get("guidance") or "rejected by user", explicit=True)
            await self._cancel()
            return dict(_DENY)
        except asyncio.CancelledError:
            if key in self.service._session_approved:
                return {"type": "allow"}
            if self._finish_reason != "process_finished":
                self.record_outcome(_ABANDONED)
                await self._cancel()
            return dict(_DENY)
        except Exception as error:
            self.record_outcome(f"Error while requesting approval: {error}", explicit=True)
            await self._cancel()
            raise

    async def _request_review(self, review, key, host, target, reason, blocked):
        run = review.run_context
        hook = run.hook_manager if run is not None else None
        if hook is not None:
            hook = hook.fork_for_turn(workspace_root=review.workspace_root)
            hook.bind_runtime(llm=review.llm, tool_registry=review.tool_registry, tool_context=review)
            result = await hook.run_permission_request("Bash", {"command": self.request.arguments["command"],
                "description": f"network-access {target}"}, reason=reason, permission_level="confirm",
                tool_call_id=f"{key.approval_id}#{self.execution_id}", session_id=review.session_id,
                permission_mode=review.permission.mode)
            if result.blocked or result.permission_decision == "deny":
                return {"action": "reject", "guidance": result.message or result.feedback or "rejected by configuration"}
            if result.permission_decision == "allow":
                return {"action": "approve"}
        guardian = run.guardian_review_service if run is not None else None
        if guardian is not None:
            action = GuardianAction.freeze({"tool": "network_access", "target": target, "host": host,
                "protocol": _WIRE_PROTOCOLS[key.protocol], "port": key.port, "trigger": deepcopy(self.trigger)},
                call_id=key.approval_id, approval_id=review.tool_call_id, turn_id=review.turn_id,
                request_digest=review.authorization_digest, policy_identity="network_access", environment_id=None)
            action = replace(action, environment_id=self.environment_id)
            receipt = await guardian.decide(action, review, strict_auto_review=review.permission.strict_auto_review,
                retry_reason=blocked, approval_reason=reason)
            if receipt is not None and (response := receipt.approval_response()) is not None:
                return response
        if review.approval_handler is None:
            return {"action": "reject", "guidance": "Network approval requires an active approval channel."}
        amendments = [
            {"network_policy_amendment": {"network_policy_amendment": {"host": host, "action": action}}}
            for action in ("allow", "deny")]
        decisions = ["approved", "approved_for_session", amendments[0], "abort"]
        await self._emit(review, "approval_request", {"tool_call_id": review.tool_call_id,
            "tool_name": "network_access", "args": {"host": host, "protocol": key.protocol, "port": key.port},
            "command": ["network-access", target], "display_command": f"network-access {target}",
            "cwd": self.cwd, "reason": reason, "network_approval_context": {"host": host, "protocol": _WIRE_PROTOCOLS[key.protocol]},
            "proposed_network_policy_amendments": [amendment["network_policy_amendment"]["network_policy_amendment"]
                for amendment in amendments],
            "available_decisions": decisions, "request_digest": review.authorization_digest,
            "policy_identity": "network_access"})
        try:
            return await review.approval_handler(review.tool_call_id)
        finally:
            await self._emit(review, "approval.cancelled", {"request_ids": [review.tool_call_id],
                "reason": self._finish_reason or "resolved"})

    async def _wait_for_review(self, review):
        owner = asyncio.current_task()
        waiter = asyncio.create_task(review)
        self._review_waiters[owner] = waiter
        try:
            result = await waiter
            self._resolved_reviews.add(owner)
            return result
        finally:
            self._review_waiters.pop(owner, None)

    def _review_ready(self, task):
        waiter = self._review_waiters.get(task)
        return task in self._resolved_reviews or waiter is not None and waiter.done() and not waiter.cancelled()

    async def finish(self, *, reason="process_finished"):
        if self._finish_task is None:
            self._finish_reason = reason
            self._finish_task = asyncio.create_task(self._finish())
        return await asyncio.shield(self._finish_task)

    async def _finish(self):
        try:
            tasks = tuple(self._tasks)
            for task in tasks:
                # Source gives an already-ready answer priority over process
                # cancellation, including its policy-commit work.
                if not task.done() and not self._review_ready(task):
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self._finish_reason != "process_finished":
                self.record_outcome(self._finish_reason)
            self.service._registrations.pop(self.execution_id, None)
            return self.outcome
        finally:
            if self.snapshot is not None:
                await self.snapshot.aclose()

    async def abort(self, reason):
        self.record_outcome(reason)
        await self._cancel()
        return await self.finish(reason=reason)

    async def observe_blocked(self, message):
        if self._finish_task is not None:
            return
        self.record_outcome(message)
        await self._cancel()
