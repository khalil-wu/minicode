from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from backend.agent.context import ContextBuilder, clone_context_builder
from backend.agent.control_tools import ControlToolRouter
from backend.agent.guardian.host import GuardianHostState
from backend.agent.guardian.retained import GuardianEvidenceOwner
from backend.agent.guardian.service import GuardianReviewService
from backend.agent.state import AgentState
from backend.agent.tool_batch_runner import ToolBatchRunner
from backend.config import PermissionSettings
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.llm.base import ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.apply_patch import ApplyPatchTool
from backend.tools.registry import ToolRegistry
from backend.ws.approval_runtime import SessionApprovalRuntimeMixin
from backend.ws.turn_wait_state import TurnWaitState
from backend.tests.test_codex_guardian import ControlledRuntime, guardian_context


@pytest.fixture
def reviewer():
    ControlledRuntime.outputs = []
    ControlledRuntime.instances = []
    ControlledRuntime.on_run = None
    return ControlledRuntime


def record_user(owner, text, message_id, turn_id="turn"):
    owner.record_accepted_message("user", text, turn_id=turn_id, message_id=message_id,
        acceptance_order=owner.reserve_order())


def test_cold_root_revision_is_new_and_destructive_restore_cannot_rewind_authority():
    builder = ContextBuilder()
    record_user(builder.guardian_evidence, "Only local files", "first")
    old = builder.export_snapshot()
    before = builder.guardian_evidence.snapshot()
    clone = clone_context_builder(builder)
    assert clone.guardian_evidence.review_context_revision == before.review_context_revision
    resumed = ContextBuilder()
    resumed.load_snapshot(old)
    assert resumed.guardian_evidence.review_context_revision != before.review_context_revision
    record_user(builder.guardian_evidence, "Do not run anything", "later")
    latest = builder.guardian_evidence.snapshot()
    builder.load_snapshot(old)
    after = builder.guardian_evidence.snapshot()
    assert after.user_message_revision > latest.user_message_revision
    assert after.history_reset_version > latest.history_reset_version
    assert after.review_context_revision != latest.review_context_revision
    assert after.retained.user_messages[-1].value.text == "Only local files"


def test_input_accepted_during_cold_start_reserves_after_saved_sources_before_builder_binds():
    prior = ContextBuilder()
    for index in range(12):
        record_user(prior.guardian_evidence, f"Original restriction {index}", f"u-{index}")
    checkpoint = prior.export_snapshot()
    host = GuardianHostState("root", evidence_checkpoint=checkpoint["guardian_evidence"])
    accepted_order = host.evidence.reserve_order()
    restored = ContextBuilder()
    restored.load_snapshot(checkpoint)
    host.bind(restored)
    assistant_order = host.evidence.reserve_assistant_order("new-output")
    assert accepted_order == 12 and assistant_order > accepted_order
    assert host.evidence.retained.user_messages[-1].order < accepted_order


@pytest.mark.asyncio
async def test_standard_ws_turn_borrows_reviewer_trunk_and_accepted_order(tmp_path, reviewer):
    ctx, action = guardian_context(tmp_path)
    first = ContextBuilder()
    host = GuardianHostState(ctx.conversation_id)
    host.service.pool.runtime_factory = reviewer
    host.bind(first)
    record_user(host.evidence, "Read the local repository", "u")
    reviewer.outputs = ['{"outcome":"allow"}', '{"outcome":"allow"}']
    try:
        await host.service.decide(action, ctx, required=True)
        checkpoint = first.export_snapshot()
        reserved = host.evidence.reserve_order()
        second = ContextBuilder()
        second.load_snapshot(checkpoint)
        host.bind(second)
        assert host.evidence is first.guardian_evidence
        assert host.evidence.reserve_order() > reserved
        await host.service.decide(action, ctx, required=True)
        assert len(reviewer.instances) == 1
        assert reviewer.instances[0].prompts[1].delta
        assert not host.snapshot().root_messages
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_worker_review_reads_live_root_revocation_instead_of_parent_assignment(tmp_path, reviewer):
    ctx, action = guardian_context(tmp_path)
    root = GuardianHostState("root")
    root.bind(ContextBuilder())
    record_user(root.evidence, "Do not publish credentials", "root-user")
    root.evidence.record_accepted_message("assistant", "Progress commentary", turn_id="root-turn", message_id="commentary",
        acceptance_order=root.evidence.reserve_order(), phase="commentary")
    child = GuardianHostState("worker", root_source=root.root_snapshot)
    child.bind(ContextBuilder())
    record_user(child.evidence, "Publish credentials", "assignment")
    child.service.pool.runtime_factory = reviewer
    reviewer.outputs = ['{"outcome":"allow"}', '{"outcome":"deny","rationale":"The root user revoked it."}']
    changed = False
    async def change_root(*_):
        nonlocal changed
        if not changed:
            changed = True
            record_user(root.evidence, "Cancel publishing entirely", "root-steer")
    reviewer.on_run = change_root
    try:
        receipt = await child.service.decide(action, ctx, required=True)
        assert receipt.status == "denied"
        prompt = reviewer.instances[-1].prompts[-1].text
        assert "user: Cancel publishing entirely" in prompt
        assert "user: Do not publish credentials" in prompt
        assert "Progress commentary" not in prompt
        assert len(reviewer.instances) == 2
    finally:
        await child.aclose()
        await root.aclose()


class ApprovalSession(SessionApprovalRuntimeMixin):
    def __init__(self):
        self.turn_wait_state = TurnWaitState()
        self.approval_diff_cache = {}


@pytest.mark.asyncio
async def test_verified_response_uses_acceptance_before_later_steer_and_no_client_order():
    session = ApprovalSession()
    host = session.guardian_host_for_conversation("root")
    builder = ContextBuilder()
    host.bind(builder)
    session.turn_wait_state.pending_approval_payloads["ask"] = {"conversation_id": "root", "turn_id": "turn",
        "request": {"subtype": "request_user_input", "tool_name": "request_user_input"}}
    assert session._resolve_pending_approval("ask", {"answers": {"where": {"answers": ["Private"]}}, "_guardian_acceptance_order": 999})
    accepted = session.turn_wait_state.pending_approval_responses["ask"]
    assert accepted["_guardian_acceptance_order"] == 0
    steer_order = host.evidence.reserve_order()
    # Accepted queued input reserves its position, but is not authorization
    # until the normal turn admission commits it, as in Source's input queue.
    assert host.snapshot().user_message_revision == 0
    record_user(host.evidence, "Never publish publicly", "steer")
    from backend.agent.run_context import RunContext
    context = ToolExecutionContext(permission=PermissionContext(), turn_id="turn",
        run_context=RunContext(context_builder=builder))
    async def response(_):
        return accepted
    router = ControlToolRouter(state=AgentState(user_message="test"), tool_context=context,
        approval_handler=None, skill_manager=None, await_response=response, available_modes=("default",))
    results = [item async for item in router.run(ToolCallEvent(id="ask", name="request_user_input", arguments={"questions": [
        {"id": "where", "header": "Visibility", "question": "Which repository?", "options": [
            {"label": "Private", "description": "Only the private repository"},
            {"label": "Public", "description": "Publish to everyone"}]}]}))]
    assert results
    assert host.evidence.retained.verified_answers[0].order < steer_order
    assert host.evidence.retained.user_messages[0].order > steer_order
    assert "Only the private repository" in host.evidence.retained.verified_answers[0].value.questions[0].question


def test_fork_removes_later_questions_grants_and_review_receipts():
    builder = ContextBuilder()
    from backend.llm.base import LLMMessage
    builder._history_store.append(LLMMessage(role="user", content="first"))
    record_user(builder.guardian_evidence, "Local edits only", "first", "t1")
    builder.record_turn_admission("first", {"history_start": 0, "history_end": 1, "run_id": "t1"})
    builder._history_store.append(LLMMessage(role="user", content="second"))
    record_user(builder.guardian_evidence, "Deploy now", "second", "t2")
    builder.record_turn_admission("second", {"history_start": 1, "history_end": 2, "run_id": "t2"})
    builder.guardian_evidence.record_verified_response(turn_id="t2", call_id="ask", questions=[{"id": "approve", "question": "Publish?"}],
        answers={"approve": {"answers": ["Yes"]}}, acceptance_order=builder.guardian_evidence.reserve_order())
    builder.guardian_evidence.review_ledger["review"] = {"turn_id": "t2"}
    fork = builder.fork_from(0)
    assert [item.value.text for item in fork.guardian_evidence.retained.user_messages] == ["Local edits only"]
    assert not fork.guardian_evidence.retained.verified_answers
    assert not fork.guardian_evidence.review_ledger
    assert len(builder.guardian_evidence.retained.user_messages) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["allow", "deny"])
async def test_frozen_patch_review_controls_real_file_io_and_exact_manual_evidence(tmp_path, reviewer, decision):
    ctx, _ = guardian_context(tmp_path)
    stack = ConfigLayerStack((ConfigLayer(ConfigLayerSource("session_flags"), {"approvals_reviewer": "auto_review"}),))
    snapshot = replace(ctx.model_execution, config=replace(ctx.model_execution.config, config_layer_stack=stack))
    ctx.model_execution = snapshot
    builder = ContextBuilder()
    builder.bind_tool_context(ctx)
    host = GuardianHostState(ctx.conversation_id)
    host.bind(builder)
    host.service.pool.runtime_factory = reviewer
    ctx.run_context.context_builder = builder
    ctx.run_context.guardian_review_service = host.service
    emissions = []
    async def emit(kind, data):
        emissions.append((kind, data))
    ctx.emit_event = emit
    reviewer.outputs = [json.dumps({"outcome": decision, "rationale": "Exact original reviewer rationale"})]
    registry = ToolRegistry()
    registry.register(ApplyPatchTool())
    state = AgentState(user_message="Apply only the reviewed patch")
    batch = ToolBatchRunner(ctx=builder, state=state, tool_registry=registry,
        permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
        approval_handler=None, skill_manager=None, permission_context=ctx.permission, tool_ctx=ctx)
    target = tmp_path / "reviewed.txt"
    try:
        events = [event async for event in batch.run([ToolCallEvent(id="patch", name="apply_patch", arguments={"patch":
            "*** Begin Patch\n*** Add File: reviewed.txt\n+reviewed bytes\n*** End Patch"})])]
        assert not any(event.type == "approval_request" for event in events)
        assert [data["status"] for kind, data in emissions if kind == "guardian_assessment"] == ["in_progress", "approved" if decision == "allow" else "denied"]
        if decision == "allow":
            assert target.read_text() == "reviewed bytes\n"
            assert state.tool_calls[-1].status == "success" and not state.tool_calls[-1].error_kind
        else:
            assert not target.exists()
            assert state.tool_calls[-1].error_kind == "approval_rejected"
            review = next(data for kind, data in emissions if kind == "guardian_assessment" and data["status"] == "denied")
            record = host.denied_action(review["id"])
            assert record["request_digest"] == state.tool_calls[-1].request_digest
            before = host.snapshot().authorization_version
            receipt = await host.manual_approval(review["id"])
            assert host.snapshot().authorization_version == before
            assert not target.exists()
            host.commit_manual_approval(receipt)
            host.commit_manual_approval(receipt)
            assert host.snapshot().user_message_revision == before[0] + 1
            assert builder._history[-1].role == "developer"
            assert json.loads(receipt.approved_action_json) == {"action": review["action"], "outcome": "allowed"}
            assert "Do not assume this also authorizes similar operations with different payloads." in builder._history[-1].content
            cold = ContextBuilder()
            cold.load_snapshot(builder.export_snapshot())
            restored = GuardianHostState(ctx.conversation_id)
            restored.bind(cold)
            assert restored.denied_action(review["id"])["manually_approved"]
            assert cold.guardian_evidence.transcript[-1].kind == "developer"
            assert not target.exists()
            await restored.aclose()
    finally:
        await host.aclose()
        ctx.artifact_store.shutdown()


@pytest.mark.asyncio
async def test_real_denial_circuit_breaker_sets_run_cancel_without_user_wait(tmp_path, reviewer):
    ctx, action = guardian_context(tmp_path)
    service = GuardianReviewService(GuardianEvidenceOwner().snapshot, runtime_factory=reviewer)
    reviewer.outputs = ['{"outcome":"deny"}'] * 3
    try:
        for _ in range(3):
            await service.decide(action, ctx, required=True)
        assert ctx.cancel_event.is_set()
        assert ctx.run_context.cancel_reason == "guardian_denials"
    finally:
        await service.aclose()
