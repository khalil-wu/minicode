"""Source checkpoint protocols; HTTP replies are explicit local fixtures."""
from __future__ import annotations

import asyncio
import gzip
import json
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.guardian.config import build_reviewer_config, review_model_for_execution, select_review_model
from backend.agent.guardian.context import (
    GuardianCheckpointRecoveryNeeded, GuardianContextMode, GuardianParentCheckpoint,
    GuardianReviewerReuseKey, ReviewContextPolicy, checkpoint_in_history,
)
from backend.agent.guardian.contract import GuardianReviewError
from backend.agent.guardian.host import GuardianHostState, parent_window_transcript, restore_requires_parent_context
from backend.agent.guardian.prompt import GuardianPermissionEvidence, build_guardian_prompt
from backend.agent.guardian.retained import GuardianEvidenceOwner, GuardianTranscriptEntry
from backend.agent.guardian.runtime import GuardianContextBuilder, GuardianRuntime, guardian_input_messages
from backend.agent.guardian.service import GuardianReviewService
from backend.agent.model_execution import ModelExecutionSnapshot
from backend.agent.state import AgentState
from backend.agent.context_window import ContextWindowConfig
from backend.agent.token_budget_activation import TokenBudgetStartupConfig, prepare_token_budget_for_startup
from backend.config import AppConfig, LLMSettings, TokenBudget
from backend.feature_flags import FeatureFlags
from backend.llm.base import LLMMessage, ToolCallEvent, estimate_llm_message_tokens
from backend.llm.native_compaction import NATIVE_COMPACTION_TYPE, native_compaction_windows, responses_context_origin
from backend.llm.openai_adapter import OpenAIAdapter
from backend.llm.provider_contracts import ModelDefinition
from backend.tests.test_codex_guardian import ControlledRuntime, guardian_context, model_definition


def raw_checkpoint(**changes):
    return {"type": "compaction", "id": "cmp-captured", "encrypted_content": "explicit-fixture-opaque-state", **changes}


def parent_checkpoint(**changes):
    return GuardianParentCheckpoint(raw_checkpoint(**changes), responses_context_origin("http://model-fixture"), "producer-hash")


def record_user(owner, text="Only local edits", message_id="accepted-user", turn_id="accepted-turn"):
    owner.record_accepted_message("user", text, turn_id=turn_id, message_id=message_id, acceptance_order=owner.reserve_order())


@pytest.fixture
def reviewer():
    ControlledRuntime.outputs = []
    ControlledRuntime.instances = []
    ControlledRuntime.on_run = None
    return ControlledRuntime


@pytest.mark.parametrize("change,usable", [
    ({}, True), ({"id": ""}, True), ({"type": "context_compaction"}, True),
    ({"id": None}, False), ({"id": 1}, False), ({"encrypted_content": ""}, False),
    ({"encrypted_content": None}, False), ({"type": "message"}, False),
])
def test_exact_source_checkpoint_usability(change, usable):
    assert parent_checkpoint(**change).is_usable is usable


@pytest.mark.parametrize("producer,reviewer_hash,mode", [
    ("shared", "shared", "thread_owned"), ("producer", "reviewer", "legacy"),
    (None, None, "legacy"), ("", "", "legacy"), ("producer", None, "legacy"),
])
def test_checkpoint_compatibility_does_not_guess_unknown_hash(producer, reviewer_hash, mode):
    checkpoint = GuardianParentCheckpoint(raw_checkpoint(), "captured-origin", producer)
    assert GuardianContextMode.for_checkpoint(checkpoint, reviewer_hash).value == mode
    assert GuardianContextMode.for_checkpoint(None, reviewer_hash) is GuardianContextMode.THREAD_OWNED


@pytest.mark.parametrize("mode,reuse,expected,seeds", [
    ("legacy", False, "legacy", False), ("legacy", True, "legacy_with_checkpoint_reuse", True),
    ("independent", False, "independent", False), ("independent", True, "independent", False),
    ("thread_owned", True, "thread_owned", True),
])
def test_source_context_policy_seed_matrix(mode, reuse, expected, seeds):
    policy = ReviewContextPolicy.for_context(mode, reuse_parent_compaction=reuse)
    assert policy.value == expected
    checkpoint = parent_checkpoint()
    assert policy.parent_compaction(checkpoint) == (checkpoint if seeds else None)
    unusable = parent_checkpoint(id=None)
    if policy is ReviewContextPolicy.THREAD_OWNED:
        with pytest.raises(GuardianReviewError, match="unusable"):
            policy.parent_compaction(unusable)
    else:
        assert policy.parent_compaction(unusable) is None


def test_live_replacement_preserves_thread_owned_and_same_checkpoint_cut():
    owner = GuardianEvidenceOwner()
    record_user(owner)
    owner.capture_parent_compaction(raw_checkpoint(), origin="issued-origin", model_hash="producer", reviewer_compaction_hash="other")
    assert owner.context_mode == "thread_owned"
    original_cut = owner.checkpoint_transcript_start
    record_user(owner, "new current request", "later")
    owner.capture_parent_compaction(raw_checkpoint(), origin="issued-origin", model_hash="producer", reviewer_compaction_hash="other")
    assert owner.checkpoint_transcript_start == original_cut
    before = owner.snapshot()
    owner.context_mode = "legacy"
    owner.capture_parent_compaction(raw_checkpoint(id="new-compatible"), origin="issued-origin", model_hash="reviewer", reviewer_compaction_hash="reviewer")
    assert owner.context_mode == "thread_owned"
    assert owner.user_message_revision == before.user_message_revision + 1
    assert owner.history_reset_version == owner.history_version


def test_checkpoint_round_trip_detaches_raw_payload_and_preserves_cold_provenance():
    raw = raw_checkpoint()
    owner = GuardianEvidenceOwner(has_transcript_backup=False)
    record_user(owner)
    owner.capture_parent_compaction(raw, origin="captured-origin", model_hash="captured-hash", reviewer_compaction_hash="captured-hash")
    raw["encrypted_content"] = "mutated-later-parent"
    persisted = json.loads(json.dumps(owner.checkpoint()))
    restored = GuardianEvidenceOwner.from_checkpoint(persisted)
    before = restored.snapshot()
    restored.restore_review_context(reviewer_compaction_hash="new-reviewer")
    assert restored.context_mode == "legacy"
    assert restored.parent_compaction.payload() == owner.parent_compaction.payload()
    assert restored.parent_compaction.item["encrypted_content"] == "explicit-fixture-opaque-state"
    assert restored.checkpoint_transcript_start == before.checkpoint_transcript_start
    assert restored.history_version == before.history_version
    assert restored.user_message_revision == before.user_message_revision
    assert not restored.has_transcript_backup
    again = GuardianEvidenceOwner.from_checkpoint(restored.checkpoint())
    assert not again.has_transcript_backup


def test_latest_malformed_checkpoint_is_not_replaced_with_older_usable_item():
    builder = ContextBuilder()
    latest = raw_checkpoint(id=None, encrypted_content="new-unusable-state")
    window = {"type": NATIVE_COMPACTION_TYPE, "origin": "actual-origin", "compaction_model_hash": "actual-producer",
        "output": [raw_checkpoint(id="older-usable"), latest]}
    builder._install_compacted_history([LLMMessage(role="assistant", provider_items=[window])], removed_prefix=0, recent_count=0, restore_state=None)
    assert builder.guardian_evidence.parent_compaction.item == latest
    with pytest.raises(GuardianReviewError, match="unusable"):
        ReviewContextPolicy.THREAD_OWNED.parent_compaction(builder.guardian_evidence.parent_compaction)
    assert ReviewContextPolicy.LEGACY_WITH_CHECKPOINT_REUSE.parent_compaction(builder.guardian_evidence.parent_compaction) is None


@pytest.mark.parametrize("backup,gap,answer,matched,expected", [
    (True, True, False, False, False), (True, False, True, False, False),
    (False, True, False, True, True), (False, False, True, True, True),
    (False, False, False, True, False), (False, False, False, False, True),
])
def test_restore_dependency_uses_actual_backup_and_exact_surviving_source(backup, gap, answer, matched, expected):
    builder = ContextBuilder()
    owner = builder.guardian_evidence
    owner.has_transcript_backup = backup
    record_user(owner)
    owner.retained.verified_answers_incomplete = gap
    if answer:
        owner.record_verified_response(turn_id="accepted-turn", call_id="question", questions=[{"id": "a", "question": "Scope?"}],
            answers={"a": {"answers": ["Private"]}}, acceptance_order=owner.reserve_order())
    builder._history_store.append(LLMMessage(role="assistant", provider_items=[{"type": "message", "id": "accepted-user" if matched else "wrong-id",
        "role": "user", "internal_chat_message_metadata_passthrough": {"turn_id": "accepted-turn"},
        "content": [{"type": "input_text", "text": "Only local edits"}]}]))
    assert restore_requires_parent_context(owner, builder) is expected


@pytest.mark.parametrize("field,value", [("turn_id", "other-turn"), ("role", "assistant"), ("text", "Different instruction")])
def test_restoration_does_not_equate_matching_text_or_id_with_complete_source(field, value):
    builder = ContextBuilder()
    owner = builder.guardian_evidence
    owner.has_transcript_backup = False
    record_user(owner)
    source = {"turn_id": "accepted-turn", "role": "user", "text": "Only local edits", field: value}
    item = {"type": "message", "id": "accepted-user", "role": source["role"],
        "internal_chat_message_metadata_passthrough": {"turn_id": source["turn_id"]},
        "content": [{"type": "input_text", "text": source["text"]}]}
    builder._history_store.append(LLMMessage(role="assistant", provider_items=[item]))
    assert restore_requires_parent_context(owner, builder)


def test_missing_backup_survives_new_save_and_user_incomplete_alone_does_not_force_parent():
    payload = GuardianEvidenceOwner().checkpoint()
    payload.pop("transcript")
    payload.pop("has_transcript_backup")
    owner = GuardianEvidenceOwner.from_checkpoint(payload)
    owner.retained.user_messages_incomplete = True
    assert not owner.has_transcript_backup
    assert not restore_requires_parent_context(owner, ContextBuilder())
    assert not GuardianEvidenceOwner.from_checkpoint(owner.checkpoint()).has_transcript_backup


@pytest.mark.parametrize("backup,producer_hash,reuse,expected_mode", [
    (True, None, True, "legacy"), (True, "producer-hash", True, "legacy"), (True, "reviewer-hash", True, "thread_owned"),
    (False, None, True, "thread_owned"), (False, "producer-hash", True, "thread_owned"),
    (False, None, False, "independent"), (False, "producer-hash", False, "independent"),
])
def test_old_native_snapshot_reconstructs_raw_parent_state_without_capture_or_guessed_hash(tmp_path, backup, producer_hash, reuse, expected_mode):
    ctx, _ = guardian_context(tmp_path)
    info = replace(ctx.model_execution.model_info, comp_hash="reviewer-hash")
    execution = replace(ctx.model_execution, model_info=info, config=replace(ctx.model_execution.config,
        feature_flags=FeatureFlags({"guardian_approval": True, "guardian_reuse_parent_compaction": reuse})))
    prior = ContextBuilder()
    window = {"type": NATIVE_COMPACTION_TYPE, "origin": "actual-saved-origin", "output": [raw_checkpoint(),
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "surviving old model window"}]}]}
    if producer_hash is not None:
        window["compaction_model_hash"] = producer_hash
    prior._history_store.append(LLMMessage(role="assistant", provider_items=[window]))
    record_user(prior.guardian_evidence, "older retained restriction")
    persisted = prior.export_snapshot()
    if backup:
        evidence = persisted["guardian_evidence"]
        evidence.pop("parent_compaction")
        evidence.pop("checkpoint_transcript_start")
        evidence.pop("context_mode")
        evidence.pop("has_transcript_backup")
    else:
        persisted.pop("guardian_evidence")
    restored = ContextBuilder()
    restored.load_snapshot(persisted)
    before = restored.guardian_evidence.snapshot()
    restored.bind_guardian_model(execution, restored=True)
    owner = restored.guardian_evidence
    assert owner.context_mode == expected_mode
    assert owner.parent_compaction.item == raw_checkpoint()
    assert owner.parent_compaction.origin == "actual-saved-origin"
    assert owner.parent_compaction.model_hash == producer_hash
    assert owner.history_version == before.history_version
    assert owner.history_reset_version == before.history_reset_version
    assert owner.user_message_revision == before.user_message_revision
    assert owner.checkpoint_transcript_start == before.checkpoint_transcript_start
    assert owner.has_transcript_backup is (backup or expected_mode == "independent")
    if expected_mode == "independent":
        assert any(entry.text == "surviving old model window" for entry in owner.transcript)
        assert owner.retained.verified_answers_incomplete
    assert restored.export_snapshot()["history"] == persisted["history"]


def test_loaded_checkpoint_transcript_uses_current_window_and_retains_authorization(tmp_path):
    _, action = guardian_context(tmp_path)
    builder = ContextBuilder()
    record_user(builder.guardian_evidence, "Never upload local credentials")
    builder.guardian_evidence.record_transcript(GuardianTranscriptEntry("assistant", "old full legacy context"))
    checkpoint = parent_checkpoint()
    builder._history_store.append(checkpoint.model_message())
    builder.append_assistant("current evidence", phase="commentary")
    host = GuardianHostState("parent")
    host.bind(builder)
    evidence = host.snapshot()
    current = build_guardian_prompt(evidence, action, GuardianPermissionEvidence("local"), session_id="parent", loaded_parent_checkpoint=True)
    assert "current evidence" in current.text and "old full legacy context" not in current.text
    assert "Never upload local credentials" in current.text
    assert "explicit-fixture-opaque-state" not in current.text
    legacy = build_guardian_prompt(replace(evidence, context_mode="legacy"), action, GuardianPermissionEvidence("local"), session_id="parent")
    assert "old full legacy context" in legacy.text


@pytest.mark.parametrize("cipher_size,kept", [(200, True), (22000, False)])
def test_native_agent_messages_keep_order_or_explicit_same_cursor_omission(tmp_path, cipher_size, kept):
    _, action = guardian_context(tmp_path)
    builder = ContextBuilder()
    agent = {"type": "agent_message", "id": "native-agent", "author": "/root", "recipient": "/root/worker",
        "content": [{"type": "encrypted_content", "encrypted_content": "x" * cipher_size}]}
    builder._history_store.append(LLMMessage(role="assistant", provider_items=[
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "before native"}]}, agent,
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "after native"}]},
        {"type": "custom_tool_call", "call_id": "custom", "name": "apply_patch", "input": "original patch input"},
        {"type": "custom_tool_call_output", "call_id": "custom", "output": [{"type": "input_text", "text": "real patch result"}, {"type": "input_image", "image_url": "image-only"}]},
        {"type": "tool_search_call", "call_id": "search", "arguments": "not-source-transcript"},
    ]))
    entries = parent_window_transcript(builder)
    assert len(entries) == 5
    snapshot = replace(builder.guardian_evidence.snapshot(), parent_window_transcript=entries)
    prompt = build_guardian_prompt(snapshot, action, GuardianPermissionEvidence("local"), session_id="parent")
    assert prompt.cursor.transcript_entry_count == 5
    assert "tool apply_patch call: original patch input" in prompt.text
    assert "tool apply_patch result: real patch result" in prompt.text
    assert "not-source-transcript" not in prompt.text and "image-only" not in prompt.text
    messages = guardian_input_messages(prompt.items)
    native = [message for message in messages if message.provider_items]
    if kept:
        assert len(messages) == 3 and native[0].provider_items == [agent]
        assert "before native" in messages[0].content and "after native" in messages[2].content
        assert agent["content"][0]["encrypted_content"] not in prompt.text
    else:
        assert not native and "[2] assistant: Some conversation entries were omitted." in prompt.text


def test_guardian_config_explicitly_disables_inherited_and_model_default_token_budget(tmp_path):
    ctx, _ = guardian_context(tmp_path)
    parent = replace(ctx.model_execution.config, context_window_config=ContextWindowConfig(model_context_window=12000,
        use_history_notes_extension=True, guidance_message="parent-owned guidance"),
        token_budget_startup_config=TokenBudgetStartupConfig(True, ContextWindowConfig(use_history_notes_extension=True)))
    info = replace(ctx.model_execution.model_info, extra={"model_messages": {"token_budget": {"enabled": True}}})
    reviewer_model = select_review_model(replace(ctx.model_execution, model_info=info), ())
    config = build_reviewer_config(parent, reviewer_model)
    assert config.token_budget_startup_config is None
    assert config.context_window_config == ContextWindowConfig(model_context_window=12000)
    prepared = prepare_token_budget_for_startup(config, info)
    assert not prepared.feature_flags.enabled("token_budget")
    assert not prepared.feature_flags.enabled("context_management")
    assert prepared.token_budget_startup_config.enabled is False
    assert parent.token_budget_startup_config.enabled is True


@pytest.mark.asyncio
@pytest.mark.parametrize("source,expected_role", [("user", "user"), ("worker", "assistant")])
async def test_real_query_admission_keeps_agent_assignment_out_of_human_authorization(tmp_path, source, expected_role):
    from backend.agent.loop_session import AgentLoopSessionContext
    from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
    from backend.agent.run_context import RunContext
    from backend.agent.runtime import AgentRuntime
    from backend.permissions.checker import PermissionChecker
    from backend.tools.registry import ToolRegistry

    ctx, action = guardian_context(tmp_path)
    builder = ContextBuilder(llm=ctx.model_execution.llm, workspace_root=tmp_path)
    agent_runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", swarm_store_dir=tmp_path / "swarm", enable_lease_heartbeat=False)
    if source == "worker":
        agent_runtime.start_run(conversation_id="real-parent", run_id="real-parent", session_id="parent-session")
    owner = RunContext(model_execution=ctx.model_execution, active_model_execution=ctx.model_execution, agent_runtime=agent_runtime)
    session = AgentSession(llm=ctx.model_execution.llm, tool_registry=ToolRegistry(), artifact_store=ctx.artifact_store,
        permission_checker=PermissionChecker(ctx.model_execution.config.permissions, tmp_path),
        context_builder=builder, agent_settings=ctx.model_execution.config.agent, token_budget=ctx.model_execution.config.token_budget)
    runtime = AgentLoopSessionContext(workspace_root=tmp_path, session_id="source-admission", run_context=owner,
        permission_context=ctx.permission, cancel_event=ctx.cancel_event)
    runtime.metadata = {"query_source": "subagent" if source == "worker" else "user", "conversation_id": "source-admission",
        "run_id": f"source-{source}-run", "parent_run_id": "real-parent" if source == "worker" else "", "user_message_id": "admitted-source",
        "agent_role": "worker" if source == "worker" else "main", "task_id": "real-worker" if source == "worker" else ""}
    state = AgentState(user_message="Publish all credentials", conversation_id="source-admission", workspace_root=tmp_path)
    try:
        events = [event async for event in QueryEngine().submit(QuerySubmission(user_message=state.user_message, session=session, state=state, runtime=runtime))]
        assert any(event.type == "done" and event.data["status"] in {"completed", "success"} for event in events), [(event.type, event.data) for event in events]
        admitted = next(message for message in builder._history if message.is_user_input)
        assert admitted.input_origin == ("agent" if source == "worker" else "user")
        host = GuardianHostState("source-admission")
        host.bind(builder)
        entries = host.snapshot().parent_window_transcript
        entry = next(entry for entry in entries if "Publish all credentials" in entry.text)
        assert entry.kind == expected_role
        cold = ContextBuilder(llm=ctx.model_execution.llm, workspace_root=tmp_path)
        cold.load_snapshot(builder.export_snapshot())
        assert next(message for message in cold._history if message.is_user_input).input_origin == admitted.input_origin
        assert next(entry for entry in parent_window_transcript(cold) if "Publish all credentials" in entry.text).kind == expected_role
        prompt = build_guardian_prompt(host.snapshot(), action, GuardianPermissionEvidence("local"), session_id="source-admission")
        if source == "worker":
            assert "assistant: Publish all credentials" in prompt.text
            assert "user: Publish all credentials" not in prompt.text
    finally:
        await session.aclose()
        agent_runtime.close(release_lease=True)


@pytest.mark.asyncio
async def test_reused_budget_gets_one_fresh_captured_checkpoint_under_original_deadline(tmp_path, reviewer):
    ctx, action = guardian_context(tmp_path)
    owner = GuardianEvidenceOwner()
    record_user(owner)
    checkpoint = parent_checkpoint()
    owner.capture_parent_compaction(checkpoint.item, origin=checkpoint.origin, model_hash=checkpoint.model_hash)
    service = GuardianReviewService(owner.snapshot, runtime_factory=reviewer)
    reviewer.outputs = ['{"outcome":"allow"}', '{"outcome":"allow"}']
    deadlines = []
    original_run = reviewer.run
    async def tracked(runtime, prompt, **arguments):
        deadlines.append(arguments["deadline"])
        return await original_run(runtime, prompt, **arguments)
    try:
        await service.decide(action, ctx, required=True)
        reviewer.instances[0].context_builder.append_user("previous admitted review input " * 30000)
        async def compact_parent(runtime, _prompt):
            if len(reviewer.instances) == 2:
                owner.capture_parent_compaction(raw_checkpoint(id="later-parent"), origin=checkpoint.origin, model_hash=checkpoint.model_hash)
        reviewer.on_run = compact_parent
        reviewer.run = tracked
        receipt = await service.decide(action, ctx, required=True)
        assert receipt.status == "approved"
        assert len(reviewer.instances) == 2 and reviewer.instances[0].closed
        fresh = reviewer.instances[1]
        assert fresh.kwargs["parent_checkpoint"].item == checkpoint.item
        assert fresh.prompts[0].loaded_parent_checkpoint and not fresh.prompts[0].delta
        assert checkpoint_in_history(fresh.context_builder._history, checkpoint)
        assert owner.parent_compaction.item["id"] == "later-parent"
        assert len(deadlines) == 1
        assert not reviewer.outputs
    finally:
        reviewer.run = original_run
        await service.aclose()


@pytest.mark.asyncio
async def test_runtime_compaction_signal_recovery_reuses_deadline_exactly_once(tmp_path, reviewer):
    ctx, action = guardian_context(tmp_path)
    owner = GuardianEvidenceOwner()
    checkpoint = parent_checkpoint()
    owner.capture_parent_compaction(checkpoint.item, origin=checkpoint.origin, model_hash=checkpoint.model_hash)
    service = GuardianReviewService(owner.snapshot, runtime_factory=reviewer)
    reviewer.outputs = ['{"outcome":"allow"}', GuardianCheckpointRecoveryNeeded(), '{"outcome":"allow"}']
    deadlines = []
    original_run = reviewer.run
    async def tracked(runtime, prompt, **arguments):
        deadlines.append(arguments["deadline"])
        return await original_run(runtime, prompt, **arguments)
    reviewer.run = tracked
    try:
        await service.decide(action, ctx, required=True)
        assert (await service.decide(action, ctx, required=True)).status == "approved"
        assert len(reviewer.instances) == 2
        assert deadlines[1] == deadlines[2]
    finally:
        reviewer.run = original_run
        await service.aclose()


@pytest.mark.asyncio
async def test_oversized_fresh_checkpoint_fails_without_second_restart_or_summary(tmp_path, reviewer):
    ctx, action = guardian_context(tmp_path)
    owner = GuardianEvidenceOwner()
    owner.capture_parent_compaction(raw_checkpoint(encrypted_content="x" * 800000), origin="real-origin", model_hash="real-producer")
    before = owner.checkpoint()
    service = GuardianReviewService(owner.snapshot, runtime_factory=reviewer)
    try:
        receipt = await service.decide(action, ctx, required=True)
        assert receipt.status == "denied" and "input budget" in receipt.rationale
        assert len(reviewer.instances) == 1 and reviewer.instances[0].closed
        assert not reviewer.instances[0].prompts
        assert owner.checkpoint() == before
    finally:
        await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidation", ["configuration", "reset", "model", "root_revision"])
async def test_reviewer_invalidation_matches_source_captured_configuration(tmp_path, reviewer, invalidation):
    ctx, action = guardian_context(tmp_path)
    owner = GuardianEvidenceOwner()
    state = {"root": ("root", 0, 0, 1, True)}
    def evidence():
        return owner.snapshot(root_versions=state["root"])
    service = GuardianReviewService(evidence, runtime_factory=reviewer)
    reviewer.outputs = ['{"outcome":"allow"}', '{"outcome":"allow"}']
    try:
        await service.decide(action, ctx, required=True)
        if invalidation == "configuration":
            cfg = replace(ctx.model_execution.config, feature_flags=FeatureFlags({"guardian_approval": True, "current_time_reminder": True}))
            ctx.model_execution = replace(ctx.model_execution, config=cfg)
        elif invalidation == "reset":
            owner.reset_history(owner.checkpoint())
        elif invalidation == "model":
            info = replace(ctx.model_execution.model_info, id="new-reviewer")
            ctx.model_execution = replace(ctx.model_execution, model="new-reviewer", model_info=info)
        else:
            state["root"] = ("root", 0, 0, 2, True)
        assert (await service.decide(action, ctx, required=True)).status == "approved"
        assert len(reviewer.instances) == 2 and reviewer.instances[0].closed
        assert not reviewer.instances[1].prompts[0].delta
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_cursorless_busy_fork_starts_from_parent_checkpoint_and_stays_ephemeral(tmp_path, reviewer):
    ctx, action = guardian_context(tmp_path)
    owner = GuardianEvidenceOwner()
    checkpoint = parent_checkpoint()
    owner.capture_parent_compaction(checkpoint.item, origin=checkpoint.origin, model_hash=checkpoint.model_hash)
    service = GuardianReviewService(owner.snapshot, runtime_factory=reviewer)
    reviewer.outputs = ['{"outcome":"allow"}'] * 3
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold_trunk(runtime, _prompt):
        if runtime is reviewer.instances[0] and len(runtime.prompts) == 2:
            entered.set()
            await release.wait()
    slow = None
    try:
        await service.decide(action, ctx, required=True)
        service.pool.trunk.cursor = None  # Source compacted fork snapshot has no transcript progress.
        reviewer.on_run = hold_trunk
        slow = asyncio.create_task(service.decide(action, ctx, required=True))
        await asyncio.wait_for(entered.wait(), 5)
        assert (await service.decide(action, ctx, required=True)).status == "approved"
        fork = reviewer.instances[1]
        assert fork.kwargs["seed_context"] is None
        assert fork.kwargs["parent_checkpoint"].item == checkpoint.item
        assert fork.prompts[0].loaded_parent_checkpoint and not fork.prompts[0].delta
        assert fork.closed
        assert service.pool.trunk.runtime is reviewer.instances[0]
        release.set()
        assert (await slow).status == "approved"
    finally:
        release.set()
        if slow is not None and not slow.done():
            await slow
        await service.aclose()


@pytest.mark.asyncio
async def test_reviewer_own_native_compaction_invalidates_delta_cursor_without_parent_seed(tmp_path, reviewer):
    import httpx
    from backend.tests.test_native_compaction import Endpoint, settings

    ctx, action = guardian_context(tmp_path)
    owner = GuardianEvidenceOwner(context_mode="independent")
    service = GuardianReviewService(owner.snapshot, runtime_factory=reviewer)
    reviewer.outputs = ['{"outcome":"allow"}'] * 2
    endpoint = Endpoint()
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        adapter = OpenAIAdapter(settings(), http_client=client)
        try:
            await service.decide(action, ctx, required=True)
            builder = reviewer.instances[0].context_builder
            builder.bind_llm(adapter)
            await builder.compact()
            assert builder._compaction_count == 1 and endpoint.requests[0][0].endswith("/compact")
            assert (await service.decide(action, ctx, required=True)).status == "approved"
            assert len(reviewer.instances) == 1
            assert not reviewer.instances[0].prompts[1].delta
            assert service.pool.trunk.completed_reviews == 2
        finally:
            await service.aclose()


@pytest.mark.asyncio
async def test_without_parent_checkpoint_budget_failure_never_creates_fake_checkpoint(tmp_path, reviewer):
    ctx, action = guardian_context(tmp_path)
    owner = GuardianEvidenceOwner(context_mode="independent")
    service = GuardianReviewService(owner.snapshot, runtime_factory=reviewer)
    reviewer.outputs = ['{"outcome":"allow"}']
    try:
        await service.decide(action, ctx, required=True)
        reviewer.instances[0].context_builder.append_user("prior request " * 100000)
        receipt = await service.decide(action, ctx, required=True)
        assert receipt.status == "denied" and "input budget" in receipt.rationale
        assert len(reviewer.instances) == 1
        assert reviewer.instances[0].kwargs["parent_checkpoint"] is None
    finally:
        await service.aclose()


@contextmanager
def source_protocol_http_fixture(output):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            if self.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            elif self.headers.get("Content-Encoding") == "zstd":
                import zstandard
                raw = zstandard.ZstdDecompressor().decompress(raw)
            body = json.loads(raw)
            requests.append({"path": self.path, "body": body, "headers": dict(self.headers)})
            if self.path.endswith("/compact"):
                content_type = "application/json"
                payload = json.dumps({"id": "compact-fixture", "object": "response.compaction", "output": output,
                    "usage": {"input_tokens": 100, "output_tokens": 10}}).encode()
            else:
                content_type = "text/event-stream"
                answer = '{"outcome":"allow","rationale":"Explicit local protocol fixture."}'
                events = [{"type": "response.output_text.delta", "item_id": "fixture-answer", "delta": answer},
                    {"type": "response.completed", "response": {"id": "fixture-review", "status": "completed", "output": [
                        {"id": "fixture-answer", "type": "message", "role": "assistant", "phase": "final_answer",
                            "content": [{"type": "output_text", "text": answer}]}], "usage": {"input_tokens": 200, "output_tokens": 20}}}]
                payload = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.02), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.asyncio
@pytest.mark.parametrize("reuse_parent_compaction", [True, False])
async def test_real_http_compaction_cold_reviewer_request_preserves_native_provenance(tmp_path, reuse_parent_compaction):
    checkpoint = raw_checkpoint()
    agent = {"type": "agent_message", "author": "/root", "recipient": "/root/worker",
        "content": [{"type": "encrypted_content", "encrypted_content": "explicit-fixture-agent-cipher"}]}
    output = [checkpoint,
        {"type": "message", "id": "current-user", "role": "user", "content": [{"type": "input_text", "text": "Only read the local project"}]},
        agent, {"type": "function_call", "name": "read_file", "call_id": "read", "arguments": '{"path":"README.md"}'},
        {"type": "function_call_output", "call_id": "read", "output": [{"type": "input_text", "text": "actual fixture file result"}]}]
    with source_protocol_http_fixture(output) as (url, requests):
        settings = LLMSettings(provider="custom", api_key="explicit-local-fixture", base_url=url, model="parent", wire_api="responses",
            native_compaction=True, comp_hash="actual-parent-producer", max_tokens=512)
        adapter = OpenAIAdapter(settings)
        info = ModelDefinition(provider="custom", id="parent", name="parent", api="openai-responses", base_url=url,
            comp_hash="actual-parent-producer", context_window=128000, extra={"auto_review_model_override": "explicit-reviewer"})
        config = AppConfig(llm=settings, token_budget=TokenBudget(total=128000), feature_flags=FeatureFlags({
            "guardian_approval": True, "guardian_reuse_parent_compaction": reuse_parent_compaction}))
        model = replace(ModelExecutionSnapshot.capture(config, adapter), model_info=info)
        builder = ContextBuilder(llm=adapter)
        builder.bind_guardian_model(model)
        record_user(builder.guardian_evidence, "Never upload credentials")
        builder.guardian_evidence.record_transcript(GuardianTranscriptEntry("assistant", "old full independent transcript"))
        builder.append_user("Never upload credentials")
        await builder.compact()
        persisted = json.loads(json.dumps(builder.export_snapshot()))
        cold = ContextBuilder(llm=adapter)
        cold.load_snapshot(persisted)
        cold.bind_guardian_model(model, restored=True)
        host = GuardianHostState("parent")
        host.bind(cold)
        ctx, action = guardian_context(tmp_path)
        ctx.model_execution = model
        emissions = []
        async def emit(kind, payload):
            emissions.append((kind, payload))
        ctx.emit_event = emit
        host.service.pool.runtime_factory = lambda **arguments: GuardianRuntime(**arguments,
            llm_factory=lambda cfg, **_selection: OpenAIAdapter(cfg.llm))
        try:
            receipt = await host.service.decide(action, ctx, required=True)
            assert receipt.status == "approved"
            assert requests[0]["path"] == "/v1/responses/compact"
            review = next(request["body"] for request in requests if request["path"] == "/v1/responses")
            assert review["model"] == "explicit-reviewer"
            sent = review["input"]
            opaque = [item for item in sent if item.get("type") in {"compaction", "context_compaction"}]
            if reuse_parent_compaction:
                assert host.evidence.context_mode == "legacy"
                assert opaque == [checkpoint]
                assert agent in sent
                public_text = "\n".join(part["text"] for item in sent if item.get("role") == "user"
                    for part in (item["content"] if isinstance(item["content"], list) else [{"text": item["content"]}]))
                assert "actual fixture file result" in public_text and "Only read the local project" in public_text
                assert "old full independent transcript" not in public_text
                assert "Never upload credentials" in public_text
            else:
                assert host.evidence.context_mode == "independent" and not opaque
                assert "old full independent transcript" in json.dumps(sent)
                assert agent not in sent
            captured = host.evidence.parent_compaction
            assert captured.origin == responses_context_origin(url)
            assert captured.model_hash == "actual-parent-producer"
            assert captured.item == checkpoint
            assert cold.export_snapshot()["guardian_evidence"]["parent_compaction"] == persisted["guardian_evidence"]["parent_compaction"]
            assert "explicit-fixture-opaque-state" not in json.dumps(emissions)
            assert "explicit-fixture-agent-cipher" not in json.dumps(emissions)
            assert [event[1]["status"] for event in emissions if event[0] == "guardian_assessment"] == ["in_progress", "approved"]
        finally:
            await host.aclose()
            await adapter.aclose()
