"""Source synchronous reviewer routing, lifetime, freshness and authorization receipts."""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from collections import deque
from dataclasses import replace
from typing import Any, Callable
from uuid import uuid4

from backend.agent.guardian.action import GuardianAction
from backend.agent.guardian.config import (build_reviewer_config, reviewer_sandbox_policy, review_model_for_execution,
    parent_has_full_access, parent_permission_profile, parent_environment_identity, reviewer_permission_profile)
from backend.agent.guardian.contract import (
    GUARDIAN_MAX_ATTEMPTS, GUARDIAN_REVIEW_TIMEOUT, INPUT_BUDGET_MESSAGE,
    REVIEW_FAILURE_INSTRUCTIONS, GuardianReceipt, GuardianReviewError, parse_guardian_assessment,
)
from backend.agent.guardian.pool import GuardianReviewerPool
from backend.agent.guardian.prompt import GuardianPermissionEvidence, build_guardian_policy, build_guardian_prompt
from backend.agent.guardian.node_repl import evidence_mode
from backend.agent.guardian.retained import GuardianEvidenceSnapshot
from backend.agent.guardian.runtime import GuardianRuntime
from backend.agent.guardian.context import (
    GuardianCheckpointRecoveryNeeded, GuardianReviewerReuseKey, ReviewContextPolicy, checkpoint_in_history,
)
from backend.agent.guardian.truncation import source_token_estimate
from backend.agent.loop_preflight import PhaseDeadlineExceeded, await_preflight
from backend.agent.run_context import RunContext
from backend.llm.base import estimate_llm_context_tokens

REJECTION_INSTRUCTIONS = (
    "The agent must not attempt to achieve the same outcome via workaround, indirect execution, or policy circumvention. "
    "Proceed only with a materially safer alternative, or if the user explicitly approves the action after being informed of the risk. "
    "Otherwise, stop and request user input."
)


class GuardianReviewService:
    def __init__(self, evidence_source: Callable[[], GuardianEvidenceSnapshot], *, runtime_factory=GuardianRuntime,
        interruption_handler=None, review_ledger_source=None):
        self.evidence_source = evidence_source
        self.pool = GuardianReviewerPool(runtime_factory)
        self.interruption_handler = interruption_handler
        self.reviews: deque[dict[str, Any]] = deque(maxlen=8)
        self._review_ledger: dict[str, dict[str, Any]] = {}
        self.review_ledger_source = review_ledger_source
        self.denials: dict[str, dict[str, Any]] = {}

    @property
    def review_ledger(self) -> dict[str, dict[str, Any]]:
        return self.review_ledger_source() if self.review_ledger_source is not None else self._review_ledger

    async def _emit(self, context, event_type: str, payload: dict[str, Any]):
        if context.emit_event is not None:
            await context.emit_event(event_type, {**payload, "conversation_id": context.conversation_id,
                "message_id": context.metadata.get("assistant_message_id", "")})

    async def decide(self, action: GuardianAction, context, *, required: bool = False, strict_auto_review: bool = False,
        retry_reason: str | None = None, approval_reason: str | None = None, approvals_reviewer: str | None = None,
        require_synchronous_review: bool = False, model_requires_review: bool = False) -> GuardianReceipt | None:
        parent = context.model_execution
        if parent is None:
            if not required and not strict_auto_review and approvals_reviewer != "auto_review":
                return None
            raise GuardianReviewError("prompt_build", "Guardian requires the issuing model execution snapshot")
        settings = parent.config.config_layer_stack.effective_config() if parent.config.config_layer_stack is not None else {}
        if parent.config.config_layer_stack is not None:
            requirements = parent.config.config_layer_stack.requirements
            issuing_model = parent.model_info.id if parent.model_info is not None else parent.model
            model_requires_review = model_requires_review or requirements.auto_review_required_for_model(issuing_model)
            required = required or model_requires_review or not requirements.can_select_user_reviewer()
        reviewer = approvals_reviewer or settings.get("approvals_reviewer", "user")
        policy = context.permission.approval_policy
        routed = policy in {"on-request", "granular"} and reviewer == "auto_review"
        if not (required or strict_auto_review or model_requires_review or routed):
            return None
        require_fresh_review = (require_synchronous_review or model_requires_review and not parent.config.feature_flags.enabled("guardianv2")
            or retry_reason is not None or action.kind == "exec_command" and action.payload.get("sandbox_permissions") == "require_escalated")
        review_id = uuid4().hex
        started_at_ms = int(time.time() * 1000)
        event = {"id": review_id, "target_item_id": None if action.kind == "network_access" else action.call_id, "turn_id": action.turn_id,
            "started_at_ms": started_at_ms, "status": "in_progress", "action": action.assessment_action(),
            "review_reason": "fresh_required" if require_fresh_review else "policy"}
        digest = context.authorization_digest
        if digest != action.request_digest or (action.approval_id or action.call_id) != context.tool_call_id:
            raise GuardianReviewError("prompt_build", "the Guardian action does not match the frozen issuing request")
        if action.environment_id is not None and action.environment_id != context.environment_id:
            raise GuardianReviewError("prompt_build", "automatic approval review cannot access the request's environment")
        if context.cancel_event is not None and context.cancel_event.is_set():
            raise asyncio.CancelledError
        if parent_has_full_access(context):
            return GuardianReceipt(review_id, action.request_digest, action.policy_identity, action.turn_id, action.environment_id, "approved")
        asynchronous = context.run_context.guardian_async_service if context.run_context is not None else None
        if asynchronous is not None:
            cached = await asynchronous.cached_receipt(action, context, required=required or model_requires_review,
                strict_auto_review=strict_auto_review, require_fresh_review=require_fresh_review)
            if cached is not None:
                return cached
        await self._emit(context, "guardian_assessment", event)
        deadline = time.monotonic() + GUARDIAN_REVIEW_TIMEOUT
        if context.deadline_monotonic is not None:
            deadline = min(deadline, context.deadline_monotonic)
        lifetime = context.run_context or RunContext()
        initial = self.evidence_source()
        assessment = None
        evidence = None
        error = None
        try:
            assessment, evidence = await await_preflight(self._review(action, context, initial, settings,
                deadline, retry_reason or approval_reason), deadline=deadline, cancel_event=context.cancel_event,
                run_context=lifetime, llm=parent.llm)
        except PhaseDeadlineExceeded:
            error = GuardianReviewError("timeout")
        except asyncio.CancelledError:
            error = GuardianReviewError("cancelled")
        except GuardianReviewError as exc:
            error = exc
        except Exception as exc:
            # Source's session/config adapter returns an explicit error result
            # at this boundary. It must settle the started assessment too.
            error = GuardianReviewError("session", str(exc))
        if context.cancel_event is not None and context.cancel_event.is_set():
            assessment = evidence = None
            error = GuardianReviewError("cancelled")
        if error is not None:
            if error.kind == "cancelled":
                receipt = GuardianReceipt(review_id, action.request_digest, action.policy_identity, action.turn_id, action.environment_id, "aborted")
            elif error.kind == "timeout":
                receipt = GuardianReceipt(review_id, action.request_digest, action.policy_identity, action.turn_id, action.environment_id, "timed_out",
                    rationale="Automatic approval review timed out while evaluating the requested approval.")
            elif error.kind == "input_budget" and not required and not strict_auto_review and not model_requires_review:
                receipt = GuardianReceipt(review_id, action.request_digest, action.policy_identity, action.turn_id, action.environment_id, "aborted",
                    rationale=f"Automatic approval review failed: {INPUT_BUDGET_MESSAGE}", require_user=True)
            else:
                message = "authorization changed before approval could be applied" if error.kind == "stale_authorization" else str(error)
                receipt = GuardianReceipt(review_id, action.request_digest, action.policy_identity, action.turn_id, action.environment_id, "denied",
                    rationale=f"Automatic approval review failed: {message}\n{REVIEW_FAILURE_INSTRUCTIONS}")
        else:
            receipt = GuardianReceipt(review_id, action.request_digest, action.policy_identity, action.turn_id, action.environment_id,
                "approved" if assessment.outcome == "allow" else "denied", assessment=assessment,
                rationale=assessment.rationale if assessment.outcome == "allow" else f"This action was rejected due to unacceptable risk.\nReason: {assessment.rationale}\n{REJECTION_INSTRUCTIONS}",
                authorization_version=evidence.authorization_version, review_context_revision=evidence.review_context_revision,
                history_reset_version=evidence.history_reset_version)
        completed = {**event, "completed_at_ms": int(time.time() * 1000), "status": receipt.status, "decision_source": "agent"}
        if receipt.assessment is not None:
            completed.update(risk_level=assessment.risk_level, user_authorization=assessment.user_authorization, rationale=assessment.rationale)
            self.reviews.append({"delivery_id": f"guardian_review_{uuid4().hex}", "action": action.json_payload,
                "assessment": completed, "authorization_version": receipt.authorization_version,
                "review_context_revision": receipt.review_context_revision, "root_versions": evidence.root_versions})
            warning = f"Automatic approval review {'approved' if assessment.outcome == 'allow' else 'denied'} (risk: {assessment.risk_level}, authorization: {assessment.user_authorization}): {assessment.rationale}"
        else:
            warning = receipt.rationale
            if receipt.rationale is not None:
                completed["rationale"] = receipt.rationale
        if receipt.status == "denied":
            self.review_ledger[review_id] = {"review_id": review_id,
                "conversation_id": context.conversation_id, "turn_id": action.turn_id,
                "environment_id": action.environment_id, "request_digest": action.request_digest,
                "policy_identity": action.policy_identity, "action_json": action.json_payload,
                "assessment": completed, "history_reset_version": (evidence or initial).history_reset_version}
        if warning is not None:
            await self._emit(context, "guardian_warning", {"message": warning, "turn_id": action.turn_id})
        await self._emit(context, "guardian_assessment", completed)
        await self._record_denial(context, action.turn_id, receipt.assessment)
        return receipt

    async def _review(self, action, context, initial, settings, deadline, reason):
        parent = context.model_execution
        if not parent.config.feature_flags.enabled("guardian_approval", True):
            raise GuardianReviewError("prompt_build", "Guardian extension is not installed for this thread")
        error = None
        for attempt in range(1, GUARDIAN_MAX_ATTEMPTS + 1):
            if time.monotonic() >= deadline:
                raise GuardianReviewError("timeout")
            evidence = self.evidence_source()
            if evidence.history_reset_version != initial.history_reset_version or self._root_reset(evidence) != self._root_reset(initial):
                raise GuardianReviewError("cancelled")
            review_model = review_model_for_execution(parent)
            context_policy = ReviewContextPolicy.for_context(evidence.context_mode,
                reuse_parent_compaction=parent.config.feature_flags.enabled("guardian_reuse_parent_compaction", True))
            parent_checkpoint = context_policy.parent_compaction(evidence.parent_compaction)
            config = build_reviewer_config(parent.config, review_model)
            policy = build_guardian_policy(settings, review_model.model_info)
            readonly = reviewer_sandbox_policy(context.sandbox_policy) if context.sandbox_policy is not None else None
            permissions = GuardianPermissionEvidence.from_context(context, environment_id=action.environment_id)
            profile = parent_permission_profile(context, environment_id=action.environment_id)
            readonly_profile = reviewer_permission_profile(profile) if profile is not None else None
            info = review_model.model_info
            window_override = config.context_window_config.model_context_window if config.context_window_config is not None else None
            supported = window_override if window_override is not None else int(info.context_window) if info is not None and info.context_window else min(128000, parent.config.token_budget.total)
            percent = int(info.extra.get("effective_context_window_percent", 95)) if info is not None else 95
            maximum = supported * max(0, min(100, percent)) // 100
            signature = hashlib.sha256(json.dumps({"policy": policy, "model": review_model.model, "effort": review_model.reasoning_effort,
                "provider": parent.provider, "base_url": config.llm.base_url, "sandbox": str(readonly_profile),
                "cwd": str(context.workspace_root), "reset": evidence.history_reset_version,
                "environments": parent_environment_identity(context),
                "context_mode": evidence.context_mode, "comp_hash": review_model.compaction_hash,
                "features": config.feature_flags.flags, "context_window": supported,
                "model_auto_compact_token_limit": config.context_window_config.model_auto_compact_token_limit if config.context_window_config is not None else None,
                "instructions": settings.get("developer_instructions"), "config_fingerprint": parent.config.config_layer_stack.fingerprint if parent.config.config_layer_stack is not None else None,
                "root_reset": self._root_reset(evidence)}, sort_keys=True).encode()).hexdigest()
            root_version = ((evidence.root_versions[0], self._root_auth(evidence), self._root_review(evidence))
                if context_policy.uses_root_review_version and evidence.root_versions is not None else None)
            key = GuardianReviewerReuseKey(signature, evidence.history_version,
                evidence.history_reset_version, root_version)
            config = replace(config, model_request_policy=replace(config.model_request_policy, prompt_cache_key_override=f"guardian:{context.conversation_id}"))
            runtime_arguments = {"parent_snapshot": parent, "config": config, "policy": policy, "sandbox_policy": readonly,
                "artifacts": context.artifact_store, "conversation_id": context.conversation_id,
                "parent_checkpoint": parent_checkpoint}
            def prompt_factory(cursor, builder):
                loaded = checkpoint_in_history(builder._history, parent_checkpoint)
                existing = estimate_llm_context_tokens(builder._render_prompt_messages(None), tools=builder.reviewer_tool_schemas)
                existing += source_token_estimate(json.dumps(config.model_request_policy.final_output_json_schema, separators=(",", ":")))
                node_evidence = replace(evidence, node_repl=self.evidence_source().node_repl)
                return build_guardian_prompt(node_evidence, action, permissions, session_id=context.conversation_id,
                    previous_cursor=cursor, reason=reason, max_input_tokens=maximum, existing_context_tokens=existing,
                    loaded_parent_checkpoint=loaded, node_repl_mode=evidence_mode(parent),
                    reviewed_node_repl_sequence=builder.last_admitted_node_repl_response_sequence)
            try:
                pool_arguments = {"key": key, "runtime_arguments": runtime_arguments, "prompt_factory": prompt_factory,
                    "cancel_event": context.cancel_event or asyncio.Event(), "parent_context": context, "deadline": deadline,
                    "preserve_parent_history": context_policy is not ReviewContextPolicy.THREAD_OWNED and parent_checkpoint is None}
                try:
                    text = await self.pool.review(**pool_arguments)
                except GuardianCheckpointRecoveryNeeded:
                    # Source grants one fresh attempt under this original
                    # deadline and from this action's captured checkpoint.
                    text = await self.pool.review(**pool_arguments, force_fresh=True)
                assessment = parse_guardian_assessment(text)
                if assessment.outcome == "allow":
                    latest = self.evidence_source()
                    if (latest.history_reset_version != evidence.history_reset_version or self._root_reset(latest) != self._root_reset(evidence)
                        or latest.review_context_revision != evidence.review_context_revision or self._root_review(latest) != self._root_review(evidence)):
                        raise GuardianReviewError("cancelled")
                    if latest.authorization_version != evidence.authorization_version or self._root_auth(latest) != self._root_auth(evidence):
                        raise GuardianReviewError("stale_authorization", retryable=True)
                return assessment, evidence
            except GuardianReviewError as exc:
                error = exc
                if not exc.retryable or attempt == GUARDIAN_MAX_ATTEMPTS:
                    raise
                delay = .2 * 2 ** (attempt - 1) * random.uniform(.9, 1.1)
                retry_at = max(time.monotonic() + delay, exc.retry_at or 0)
                if retry_at >= deadline:
                    raise GuardianReviewError("timeout")
                await asyncio.sleep(max(0, retry_at - time.monotonic()))
        raise error

    @staticmethod
    def _root_reset(snapshot):
        return snapshot.root_versions[:2] if snapshot.root_versions is not None else None

    @staticmethod
    def _root_auth(snapshot):
        return (snapshot.root_versions[2], snapshot.root_versions[4]) if snapshot.root_versions is not None else None

    @staticmethod
    def _root_review(snapshot):
        return snapshot.root_versions[3] if snapshot.root_versions is not None else None

    async def _record_denial(self, context, turn_id, assessment):
        state = self.denials.setdefault(turn_id, {"consecutive": 0, "recent": deque(maxlen=50), "interrupted": False})
        denied = assessment is not None and assessment.outcome == "deny"
        state["consecutive"] = state["consecutive"] + 1 if denied else 0
        state["recent"].append(denied)
        model = context.model_execution.model_info
        cyber = model is not None and model.extra.get("model_specialty") == "cyber"
        consecutive, recent = state["consecutive"], sum(state["recent"])
        if denied and not state["interrupted"] and (consecutive >= (1 if cyber else 3) or recent >= (1 if cyber else 10)):
            state["interrupted"] = True
            message = f"Automatic approval review rejected too many approval requests for this turn ({consecutive} consecutive, {recent} in the last 50 reviews); interrupting the turn."
            await self._emit(context, "guardian_warning", {"message": message, "turn_id": turn_id, "interruption": True})
            if self.interruption_handler is not None:
                await self.interruption_handler(turn_id, message)
            elif context.cancel_event is not None:
                if context.run_context is not None:
                    context.run_context.cancel_reason = "guardian_denials"
                context.cancel_event.set()

    async def aclose(self):
        await self.pool.aclose()
