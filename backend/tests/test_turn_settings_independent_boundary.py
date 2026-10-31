from __future__ import annotations

import asyncio
import json
from pathlib import Path
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from backend.agent.message import UserCommand
from backend.agent.context import ContextBuilder
from backend.api import _state
from backend.attachments.store import AttachmentStore
from backend.conversations.repository import ConversationRepository
from backend.conversations.context_delta import apply_context_snapshot_delta
from backend.main import app
from backend.llm.base import StreamEvent, StreamEventType, ToolCallEvent, UsageInfo
from backend.tools.base import BaseTool, ToolResult, ToolSchema
from backend.tests.test_user_input_presentation_chain import session_for
from tests.test_conversations import _install_noop_llm, _receive_until_event


def _packet(owner_id: str, name: str, **settings):
    return UserCommand("user_message", {
        "content": "independent admission boundary", "conversation_id": owner_id,
        "client_command_id": name, "user_message_id": f"{name}-user",
        "assistant_message_id": f"{name}-answer", **settings,
    })


async def _dispatch(session, command):
    result = await asyncio.wait_for(session.command_dispatcher._dispatch_client_command(
        command, session.connection_generation), 25)
    await asyncio.gather(*tuple(session.execution_cleanup_tasks))
    await session.event_outbox.drain_delivery()
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ["query", "admission_commit"])
async def test_rejected_plan_input_does_not_allocate_a_durable_plan_owner(tmp_path, monkeypatch, failure_phase):
    session, owner, adapter = session_for(tmp_path, monkeypatch)
    if failure_phase == "query":
        async def fail_query(_submission):
            raise OSError("controlled failure before input admission")
            yield
        monkeypatch.setattr(session.query_engine, "submit", fail_query)
    else:
        def fail_admission(*_args, **_kwargs):
            raise OSError("controlled input admission failure")
        monkeypatch.setattr(session.conversation_repo, "commit_turn_admission", fail_admission)
    try:
        await _dispatch(session, _packet(owner.id, "rejected-plan", permission_mode="plan"))
        fresh = ConversationRepository(session.conversation_repo._base_dir).get_conversation(owner.id)
        assert fresh.permission_mode == "confirm"
        assert not any(message["role"] == "user" for message in fresh.transcript)
        assert not adapter.requests
        assert "plan_slug" not in fresh.context_snapshot
    finally:
        await session.session_lifecycle.shutdown(reason="independent-plan-rejection")


@pytest.mark.asyncio
async def test_admitted_plan_input_retains_one_plan_owner_across_turns(tmp_path, monkeypatch):
    session, owner, adapter = session_for(tmp_path, monkeypatch)
    observed_paths = []
    submit = session.query_engine.submit

    async def observe(submission):
        observed_paths.append(tuple(Path(value) for value in
            submission.runtime.permission_context.filesystem_constraints.get("plan_files", [])))
        async for event in submit(submission):
            yield event

    monkeypatch.setattr(session.query_engine, "submit", observe)
    try:
        await _dispatch(session, _packet(owner.id, "first-plan", permission_mode="plan"))
        first = ConversationRepository(session.conversation_repo._base_dir).get_conversation(owner.id)
        await _dispatch(session, _packet(owner.id, "second-plan"))
        second = ConversationRepository(session.conversation_repo._base_dir).get_conversation(owner.id)
        assert len(adapter.requests) == len(observed_paths) == 2
        assert len(observed_paths[0]) == len(observed_paths[1]) == 1
        observed = {"first_slug": first.context_snapshot.get("plan_slug"),
            "second_slug": second.context_snapshot.get("plan_slug"), "paths": observed_paths}
        slug = observed_paths[0][0].stem
        assert observed == {"first_slug": slug, "second_slug": slug,
            "paths": [observed_paths[0], observed_paths[0]]}
    finally:
        await session.session_lifecycle.shutdown(reason="independent-plan-accepted")


@pytest.mark.asyncio
async def test_capacity_rejection_does_not_grant_unadmitted_cwd_attachment_access(tmp_path, monkeypatch):
    session, _, adapter = session_for(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    session.attachment_store = AttachmentStore(tmp_path / "attachment-store")
    owner = session.conversation_repo.create_conversation(title="Unbound", memory_mode="none")
    session.active_conversation_id = owner.id
    session.attachment_store.save(artifact_id="projectless-private", content="private attachment",
        metadata={"conversation_id": owner.id, "workspace_root": ""})
    artifact_id = session.artifact_store.save("private artifact", source="fixture",
        conversation_id=owner.id, workspace_root="")
    held = session.task_manager.create("hold-capacity", asyncio.Event().wait())
    session.task_manager._max_tasks = 1
    try:
        await _dispatch(session, _packet(owner.id, "capacity-grants"))
        fresh = ConversationRepository(session.conversation_repo._base_dir).get_conversation(owner.id)
        assert fresh.execution_cwd == "" and not adapter.requests
        unadmitted_access = {
            "attachment": session.attachment_store.get_payload("projectless-private", conversation_id=owner.id,
                workspace_root=str(tmp_path)) is not None,
            "artifact": session.artifact_store.get(artifact_id, conversation_id=owner.id,
                workspace_root=str(tmp_path)) is not None,
        }
        assert unadmitted_access == {"attachment": False, "artifact": False}
        assert session.attachment_store.get_payload("projectless-private", conversation_id=owner.id,
            workspace_root="") is not None
    finally:
        session.task_manager.cancel(held.id)
        await asyncio.gather(held.task, return_exceptions=True)
        await session.session_lifecycle.shutdown(reason="independent-grant-rejection")


@pytest.mark.asyncio
async def test_projection_failure_after_admission_preserves_the_consumed_input(tmp_path, monkeypatch):
    session, owner, adapter = session_for(tmp_path, monkeypatch)

    async def fail_projection(*_args, **_kwargs):
        raise OSError("controlled admitted-settings projection failure")

    monkeypatch.setattr(session, "_publish_admitted_turn_settings", fail_projection)
    try:
        await _dispatch(session, _packet(owner.id, "projection-failure", permission_mode="bypass"))
        fresh = ConversationRepository(session.conversation_repo._base_dir).get_conversation(owner.id)
        assert fresh.permission_mode == "bypass" and fresh.permission_mode_revision == 1
        assert [message["id"] for message in fresh.transcript if message["role"] == "user"] == ["projection-failure-user"]
        assert "projection-failure-user" in fresh.context_snapshot["turn_admissions"]
        results = [event for event in session.ws.messages if event.get("type") == "command.result"
            and event.get("client_command_id") == "projection-failure"]
        assert any(event.get("data", {}).get("admitted") is True for event in results)
        assert not any(event.get("data", {}).get("admitted") is False for event in results)
        assert not adapter.requests
        assert any(event.get("type") == "done" and event.get("status") == "failed"
            for event in session.ws.messages)
    finally:
        await session.session_lifecycle.shutdown(reason="independent-post-admission-projection")


@pytest.mark.asyncio
@pytest.mark.parametrize("owned", [True, False], ids=["original-owner", "foreign-owner"])
async def test_legacy_projectless_attachment_preview_does_not_publish_or_cross_owner_grants(tmp_path, monkeypatch, owned):
    attachment_dir = tmp_path / "attachment-store"
    monkeypatch.setattr("backend.attachments.store.ATTACHMENT_DATA_DIR", attachment_dir)
    session, _, adapter = session_for(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    owner = session.conversation_repo.create_conversation(title="Unbound input", memory_mode="none")
    sibling = session.conversation_repo.create_conversation(title="Private sibling", memory_mode="none")
    session.active_conversation_id = owner.id
    session.attachment_store = AttachmentStore(attachment_dir)
    original_owner = owner if owned else sibling
    private_body = "original projectless private body with distinct boundary evidence"
    attachment = {"artifact_id": "legacy-owned-input", "file_name": "private-note.txt",
        "kind": "document", "media_type": "text/plain"}
    session.attachment_store.save(artifact_id=attachment["artifact_id"], content=private_body,
        metadata={"conversation_id": original_owner.id, "workspace_root": "", "attachment": attachment})
    submit = session.query_engine.submit
    entered, release = asyncio.Event(), asyncio.Event()

    async def observe(submission):
        entered.set()
        await release.wait()
        async for event in submit(submission):
            yield event

    monkeypatch.setattr(session.query_engine, "submit", observe)
    sending = asyncio.create_task(_dispatch(session, _packet(owner.id, "legacy-scope-input", attachments=[attachment])))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        # This external reader has no admission-preview task context.
        assert session.attachment_store.get_payload(attachment["artifact_id"],
            conversation_id=owner.id, workspace_root=str(tmp_path)) is None
        release.set()
        await sending
        fresh = ConversationRepository(session.conversation_repo._base_dir).get_conversation(owner.id)
        admitted_users = [message for message in fresh.transcript if message["role"] == "user"]
        migrated_payload = session.attachment_store.get_payload(attachment["artifact_id"],
            conversation_id=owner.id, workspace_root=str(tmp_path))
        if owned:
            assert len(adapter.requests) == len(admitted_users) == 1
            assert any(private_body in str(message.content) for message in adapter.requests[0])
            assert fresh.execution_cwd == str(tmp_path) and migrated_payload is not None
        else:
            assert not adapter.requests and not admitted_users and migrated_payload is None
            assert fresh.execution_cwd == ""
        assert session.attachment_store.get_payload(attachment["artifact_id"],
            conversation_id=original_owner.id, workspace_root="") is not None
    finally:
        release.set()
        await asyncio.gather(sending, return_exceptions=True)
        await session.session_lifecycle.shutdown(reason="independent-legacy-scope-preview")


def test_model_context_delta_preserves_owner_metadata_and_removes_cleared_model_state():
    builder = ContextBuilder()
    ownership = {"plan_slug": "bright-planning-owner", "plan_file_reference": {"path": "owned-plan.md"},
        "plan_recovery_status": "available", "ui_agent_state": {"running": False},
        "ui_agent_state_revision": 7, "scheduled_task_owner": {"id": "persisted-owner"}}
    previous = {**builder.export_snapshot(), **ownership, "context_window_state": {"old": True},
        "inactive_context_windows": [{"old": True}], "history_notes_hint": {"old": True}}
    for revision in (None, 0):
        delta, _ = builder.export_snapshot_delta(previous, since_revision=revision)
        after = apply_context_snapshot_delta(previous, delta)
        assert {key: after.get(key) for key in ownership} == ownership
        assert not {"context_window_state", "inactive_context_windows", "history_notes_hint"}.intersection(after)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["raw_capture", "direct_capacity"])
@pytest.mark.parametrize("mode", ["confirm", "plan"])
async def test_unbound_local_startup_is_pure_until_input_admission(tmp_path, monkeypatch, operation, mode):
    from backend.agent.plans import get_plans_directory

    session, _, adapter = session_for(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    owner = session.conversation_repo.create_conversation(title="Unbound local start", permission_mode=mode, memory_mode="none")
    session.active_conversation_id = owner.id
    session.permission_context = session.permission_context_for_conversation(owner, source="local-boundary")
    session.attachment_store = AttachmentStore(tmp_path / "local-attachments")
    session.attachment_store.save(artifact_id="local-legacy-private", content="local private input",
        metadata={"conversation_id": owner.id, "workspace_root": ""})
    artifact_id = session.artifact_store.save("local private artifact", source="fixture",
        conversation_id=owner.id, workspace_root="")
    repository = ConversationRepository(session.conversation_repo._base_dir)
    before = repository.get_conversation(owner.id)
    plan_settings = session.config.config_layer_stack.effective_config()
    plans_dir = get_plans_directory(tmp_path, settings=plan_settings, create=False)
    plans_existed = plans_dir.exists()
    plan_entries = sorted(path.name for path in plans_dir.iterdir()) if plans_existed else []
    held = None
    try:
        if operation == "raw_capture":
            captured = session.capture_agent_run_request(owner.id)
            assert captured.workspace_root == tmp_path and captured.permission.mode == mode
            if mode == "plan":
                exact_paths = captured.permission.filesystem_constraints.get("plan_files", [])
                assert len(exact_paths) == 1 and not Path(exact_paths[0]).exists()
        else:
            held = session.task_manager.create("local-capacity-owner", asyncio.Event().wait())
            session.task_manager._max_tasks = 1
            with pytest.raises(RuntimeError, match="Task capacity reached"):
                await session.start_agent_run("local input rejected by actual capacity", conversation_id=owner.id,
                    metadata={"user_message_id": "local-capacity-user", "assistant_message_id": "local-capacity-answer"})
        assert repository.get_conversation(owner.id) == before
        assert session.conversation_repo.get_conversation(owner.id) == before
        assert session.session_lifecycle.current_workspace_root() is None
        assert not adapter.requests and not session.run_manager.run_tasks
        assert plans_dir.exists() == plans_existed
        assert (sorted(path.name for path in plans_dir.iterdir()) if plans_dir.exists() else []) == plan_entries
        assert session.attachment_store.get_payload("local-legacy-private", conversation_id=owner.id,
            workspace_root=str(tmp_path)) is None
        assert session.artifact_store.get(artifact_id, conversation_id=owner.id,
            workspace_root=str(tmp_path)) is None
        assert session.attachment_store.get_payload("local-legacy-private", conversation_id=owner.id,
            workspace_root="") is not None
        assert session.artifact_store.get(artifact_id, conversation_id=owner.id, workspace_root="") == "local private artifact"
    finally:
        if held is not None:
            session.task_manager.cancel(held.id)
            await asyncio.gather(held.task, return_exceptions=True)
        await session.session_lifecycle.shutdown(reason="independent-local-startup-purity")


@pytest.mark.asyncio
async def test_first_plan_owner_is_published_to_live_tool_permissions_for_an_already_bound_thread(tmp_path, monkeypatch):
    from backend.agent.plans import get_plan_file_path

    session, original, adapter = session_for(tmp_path, monkeypatch)
    workspace = Path(original.execution_cwd)
    trust_file = tmp_path / "trusted-workspaces.json"
    monkeypatch.setattr("backend.workspace.trust.TRUSTED_WORKSPACES_FILE", trust_file)
    trust_file.write_text(json.dumps([str(workspace)]), encoding="utf-8")
    owner = session.conversation_repo.create_conversation(title="Plan without a file owner",
        workspace_root=str(workspace), execution_cwd=str(workspace), permission_mode="plan", memory_mode="none")
    session.active_conversation_id = owner.id
    session.permission_context = session.permission_context_for_conversation(owner, source="existing-plan")
    observed = []

    class PlanPermissionProbe(BaseTool):
        name = "audit_plan_permission"
        read_only = True

        def get_schema(self):
            return ToolSchema(self.name, "Inspect this actual tool policy", {"type": "object", "properties": {}})

        async def execute(self, args, context=None):
            observed.append(tuple(context.permission.filesystem_constraints.get("plan_files", [])))
            return ToolResult("actual plan policy inspected")

    session.tool_registry.register(PlanPermissionProbe())

    async def stream(messages, tools=None, metadata=None):
        adapter.requests.append(deepcopy(messages))
        if len(adapter.requests) == 1:
            yield StreamEvent(type=StreamEventType.TOOL_CALL,
                tool_calls=[ToolCallEvent("plan-policy-call", "audit_plan_permission", {})])
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls", usage=UsageInfo())
        else:
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="plan policy checked", phase="final_answer")
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="completed", usage=UsageInfo())

    monkeypatch.setattr(adapter, "stream_chat", stream)
    try:
        await _dispatch(session, _packet(owner.id, "bound-plan-owner"))
        fresh = ConversationRepository(session.conversation_repo._base_dir).get_conversation(owner.id)
        exact_path = str(get_plan_file_path(fresh.context_snapshot["plan_slug"], workspace, create_directory=False))
        assert len(adapter.requests) == 2
        assert observed == [(exact_path,)]
        assert session.permission_context.filesystem_constraints.get("plan_files") == [exact_path]
    finally:
        await session.session_lifecycle.shutdown(reason="independent-live-plan-permission")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["internal", "resume"])
@pytest.mark.parametrize("mode", ["confirm", "plan"])
@pytest.mark.parametrize("bound", [True, False], ids=["bound", "unbound"])
async def test_real_internal_and_checkpoint_resume_admit_owned_settings_without_a_new_user(tmp_path, monkeypatch, kind, mode, bound):
    from backend.agent.checkpoint import save_checkpoint
    from backend.agent.plans import get_plan_file_path
    from backend.agent.runtime import default_runtime
    from backend.agent.state import AgentState

    session, original, adapter = session_for(tmp_path, monkeypatch)
    workspace = Path(original.execution_cwd)
    monkeypatch.chdir(workspace)
    trust_file = tmp_path / "trusted-workspaces.json"
    monkeypatch.setattr("backend.workspace.trust.TRUSTED_WORKSPACES_FILE", trust_file)
    trust_file.write_text(json.dumps([str(workspace)]), encoding="utf-8")
    retained_user = {"id": "retained-resume-user", "role": "user", "content": "resume the retained authorized request"}
    owner = session.conversation_repo.create_conversation(title="Actual internal/resume admission",
        permission_mode=mode, memory_mode="none", workspace_root=str(workspace) if bound else "",
        execution_cwd=str(workspace) if bound else "", transcript=[retained_user] if kind == "resume" else [])
    session.active_conversation_id = owner.id
    session.permission_context = session.permission_context_for_conversation(owner, source="internal-boundary")
    runtime = default_runtime()
    metadata = {"user_message_id": "must-not-create-a-new-user", "assistant_message_id": "actual-internal-answer"}
    if kind == "resume":
        source = runtime.start_run(conversation_id=owner.id, session_id="independent-checkpoint-source", role="main")
        runtime.commit_terminal(source.run_id, status="cancelled", terminal_reason="interrupted")
        retained = ContextBuilder(conversation_id=owner.id, workspace_root=workspace)
        state = AgentState(user_message=retained_user["content"], conversation_id=owner.id, workspace_root=workspace)
        await retained.start_turn(state.user_message, state)
        save_checkpoint(session_id="independent-checkpoint-source", base_dir=runtime.state_root,
            user_message=state.user_message, iterations=1, reply="", messages=[], context_snapshot=retained.export_snapshot(),
            tool_calls=[], active_skills=[], disabled_tools=set(), stopped_reason="interrupted", last_mutation_index=0,
            run_id=source.run_id, conversation_id=owner.id)
        metadata.update(resume_from_checkpoint=True, resume_checkpoint_run_id=source.run_id,
            resume_checkpoint_session_id="independent-checkpoint-source")
        content = retained_user["content"]
    else:
        runtime.parent_outbox(conversation_id=owner.id).enqueue(subagent_id="independent-hook",
            kind="async_hook", session_id=session.session_id,
            payload={"hook_id": "actual-durable-hook", "event": "task_update", "content": "durable internal continuation evidence"})
        metadata["_parent_notification_only"] = True
        content = ""
    observed_policy, before_start, restored = [], [], []

    class PolicyProbe(BaseTool):
        name = "audit_internal_policy"
        read_only = True

        def get_schema(self):
            return ToolSchema(self.name, "Observe the actual internal tool policy", {"type": "object", "properties": {}})

        async def execute(self, args, context=None):
            fresh = session.conversation_repo.get_conversation(owner.id)
            observed_policy.append({"mode": context.permission.mode,
                "paths": list(context.permission.filesystem_constraints.get("plan_files", [])),
                "cwd": fresh.execution_cwd, "slug": fresh.context_snapshot.get("plan_slug")})
            return ToolResult("actual internal policy observed")

    session.tool_registry.register(PolicyProbe())

    async def stream(messages, tools=None, metadata=None):
        adapter.requests.append(deepcopy(messages))
        if len(adapter.requests) == 1:
            yield StreamEvent(type=StreamEventType.TOOL_CALL,
                tool_calls=[ToolCallEvent("internal-policy-call", "audit_internal_policy", {})])
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls", usage=UsageInfo())
        else:
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="internal/resume completed", phase="final_answer")
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="completed", usage=UsageInfo())

    submit = session.query_engine.submit

    async def observe_submission(submission):
        async for event in submit(submission):
            if event.type == "agent.run.started":
                current = session.conversation_repo.get_conversation(owner.id)
                before_start.append((current.execution_cwd, current.context_snapshot.get("plan_slug")))
            yield event
        restored.append(submission.runtime.metadata.get("_query_engine_recovery_restored"))

    monkeypatch.setattr(adapter, "stream_chat", stream)
    monkeypatch.setattr(session.query_engine, "submit", observe_submission)
    try:
        await asyncio.wait_for(session.start_agent_run(content, conversation_id=owner.id, metadata=metadata), 25)
        await asyncio.wait_for(asyncio.gather(*tuple(session.execution_cleanup_tasks)), 25)
        await session.event_outbox.drain_delivery()
        fresh = ConversationRepository(session.conversation_repo._base_dir).get_conversation(owner.id)
        assert before_start == [(str(workspace) if bound else "", None)]
        assert len(adapter.requests) == 2 and len(observed_policy) == 1
        assert restored == [kind == "resume"]
        assert fresh.execution_cwd == str(workspace)
        assert fresh.workspace_binding_revision == (0 if bound else 1)
        assert [message["id"] for message in fresh.transcript if message["role"] == "user"] == (
            ["retained-resume-user"] if kind == "resume" else [])
        assert sum(bool(message.is_user_input) for message in adapter.requests[0]) == (1 if kind == "resume" else 0)
        paths = ([str(get_plan_file_path(fresh.context_snapshot["plan_slug"], workspace, create_directory=False))]
            if mode == "plan" else [])
        assert observed_policy == [{"mode": mode, "paths": paths, "cwd": str(workspace),
            "slug": fresh.context_snapshot.get("plan_slug")}]
        assert session.permission_context.mode == mode
        assert session.permission_context.filesystem_constraints.get("plan_files", []) == paths
        assert not any(event.get("type") == "item.started" and event.get("item", {}).get("type") == "user_message"
            for event in session.ws.messages)
        assert any(event.get("type") == "done" and event.get("status") == "completed" for event in session.ws.messages)
    finally:
        await session.session_lifecycle.shutdown(reason="independent-real-internal-resume")


@pytest.mark.timeout(60)
@pytest.mark.parametrize("initial_active", [True, False], ids=["selected-owner", "new-draft"])
@pytest.mark.parametrize("durable", [True, False], ids=["durable", "transient"])
def test_durable_ack_binds_an_owner_omitting_input_before_the_dispatch_gap(tmp_path, monkeypatch, initial_active, durable):
    _install_noop_llm(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path / "conversations", raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations", raising=False)
    session_id = f"independent-owner-gap-{initial_active}-{durable}"
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws?session_id={session_id}") as socket:
            _receive_until_event(socket, "llm.model.updated")
            session = _state.ws_manager.get_session(session_id)
            first = session.conversation_repo.create_conversation(title="A", execution_cwd=str(tmp_path), memory_mode="none")
            second = session.conversation_repo.create_conversation(title="B", execution_cwd=str(tmp_path), memory_mode="none")
            if initial_active:
                socket.send_json({"type": "conversation.switch", "conversation_id": first.id})
                _receive_until_event(socket, "conversation.switched", max_attempts=100)
            else:
                assert session.active_conversation_id is None
            schedule_name = "_schedule_durable_client_command" if durable else "_schedule_transient_client_command"
            schedule = getattr(session.command_dispatcher, schedule_name)
            held = []

            def hold_dispatch(*args, **kwargs):
                if durable or args[0].type == "user_message":
                    held.append((args, kwargs))
                else:
                    schedule(*args, **kwargs)

            monkeypatch.setattr(session.command_dispatcher, schedule_name, hold_dispatch)
            count = 1 if initial_active else 2
            user_ids = [f"owner-gap-user-{index}" for index in range(count)]
            for index, user_id in enumerate(user_ids):
                command_id = f"owner-gap-input-{index}"
                socket.send_json({"type": "user_message", "content": f"input {index} belongs to its original owner",
                    "permission_mode": "bypass", "user_message_id": user_id,
                    "assistant_message_id": f"owner-gap-answer-{index}",
                    **({"client_command_id": command_id} if durable else {})})
                if durable:
                    ack = _receive_until_event(socket, "client.command.ack", max_attempts=100)
                    assert ack["client_command_id"] == command_id
            socket.send_json({"type": "ping"})
            _receive_until_event(socket, "pong", max_attempts=100)
            assert len(held) == count
            frozen = (session.run_manager.durable_client_commands.pending_client_commands() if durable
                else [args[0] for args, _kwargs in held])
            assert len(frozen) == count
            frozen_owners = {command.data["user_message_id"]: command.data.get("conversation_id")
                or command.data.get("owner_conversation_id") for command in frozen}
            assert all(command.data["_permission_mode_selection_revision"] == 0
                and command.data["_workspace_binding_selection_revision"] == 0 for command in frozen)
            if initial_active:
                assert frozen_owners == {user_ids[0]: first.id}
            else:
                assert all(frozen_owners.values()) and len(set(frozen_owners.values())) == count
                assert not set(frozen_owners.values()).intersection({first.id, second.id})
            monkeypatch.setattr(session.command_dispatcher, schedule_name, schedule)
            socket.send_json({"type": "conversation.switch", "conversation_id": second.id})
            _receive_until_event(socket, "conversation.switched", max_attempts=100)

            async def drain_dispatch():
                if durable:
                    await session.command_dispatcher._replay_pending_client_commands(session.connection_generation)
                else:
                    for args, kwargs in held:
                        schedule(*args, **kwargs)
                tasks = tuple(session.command_dispatcher.execution_command_tasks)
                if tasks:
                    await asyncio.gather(*tasks)
                await asyncio.gather(*tuple(session.execution_cleanup_tasks))

            client.portal.call(drain_dispatch)
            repository = ConversationRepository(session.conversation_repo._base_dir)
            saved_first = repository.get_conversation(first.id)
            saved_second = repository.get_conversation(second.id)
            assert session.active_conversation_id == second.id
            assert session.permission_context.mode == saved_second.permission_mode == "confirm"
            observed = {user_id: [summary.id for summary in repository.list_conversations()
                if any(message.get("id") == user_id for message in repository.get_conversation(summary.id).transcript)]
                for user_id in user_ids}
            assert observed == {user_id: [owner_id] for user_id, owner_id in frozen_owners.items()}
            assert not any(message["role"] == "user" for message in saved_second.transcript)
            if not initial_active:
                assert not any(message["role"] == "user" for message in saved_first.transcript)
