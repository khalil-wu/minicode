"""Detached Agent execution for project-scoped scheduled tasks.

The WebSocket session runner is intentionally tied to a live client.  A
scheduled task must still work after the desktop window is closed, so this
module runs the same QueryEngine path without borrowing a connected session.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from backend.agent.conversation_query_guard import (
    ConversationQueryClaim,
    conversation_query_guards,
)
from backend.agent.run_context import RunContext
from backend.agent.turn_input import ScheduledInputCommand, TurnInputQueue
from backend.agent.execution_journal import execution_journal_owner
from backend.agent.runtime import default_runtime
from backend.async_cleanup import to_thread_cancel_safe
from backend.conversations.repository import ConversationRepository
from backend.services.chat_api_service import run_owned_rest_chat
from backend.services.conversation_projection_service import replay_pending_conversation_projections
from backend.services.workspace_service import git_branch_for, main_worktree_root

HEARTBEAT_TERMINAL_WAIT_SECONDS = 600


def _conversation_execution_root(conversation: Any) -> str:
    return str(conversation.execution_cwd or conversation.worktree_path or conversation.workspace_root or "").strip()


def heartbeat_block_reason(task: Any, *, bootstrap: Any = None, manual: bool = False) -> str | None:
    """Read the actual local thread owner before the scheduler consumes a fire."""
    guards = conversation_query_guards()
    if guards.admission_closed:
        return "server_draining"
    manager = getattr(bootstrap, "ws_manager", None)
    if manager is not None and manager._shutting_down:
        return "server_draining"
    repository = ConversationRepository()
    conversation = repository.get_conversation(task.conversation_id)
    if conversation is None or conversation.archived:
        return "thread_missing"
    execution_root = _conversation_execution_root(conversation)
    if manager is not None:
        if manager.conversation_delete_fence(conversation.id):
            return "thread_missing"
        if not manual:
            manager.request_heartbeat_renderer_state(conversation.id, workspace_root=execution_root)
            renderer = manager.heartbeat_renderer_block_reason(conversation.id)
            if renderer is not None:
                return renderer
        owner = manager.execution_session_for_conversation(conversation.id)
        if owner is not None:
            for request in owner._pending_approval_runtime_items():
                if request.get("conversation_id") != conversation.id:
                    continue
                return "waiting_on_user_input" if request.get("subtype") == "request_user_input" else "waiting_on_approval"
            if owner.run_manager.has_pending_lifecycle_cleanup(conversation.id):
                return "turn_in_progress"
    if guards.active_claim(conversation.id) is not None or guards.has_transition(conversation.id):
        return "turn_in_progress"
    from backend.config import load_config

    config = load_config(cwd=Path(execution_root))
    if not (conversation.model_selection.get("model") or config.llm.model):
        return "missing_collaboration_mode"
    return None


async def run_scheduled_task(
    task: Any,
    run: Any,
    *,
    bootstrap: Any,
    bind_conversation: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """Run one task and persist a self-contained conversation transcript."""

    management_root = str(getattr(task, "workspace_root", "") or "").strip()
    repository = ConversationRepository()
    from backend.services.scheduler_service import scheduled_permission_mode

    # Scheduled runs use the same explicit MiniCode permission tokens.
    permission_mode = scheduled_permission_mode(getattr(task, "permission_mode", "confirm"))
    requested_conversation_id = str(
        getattr(run, "conversation_id", "")
        or getattr(task, "conversation_id", "")
        or ""
    ).strip()
    conversation = repository.get_conversation(requested_conversation_id) if requested_conversation_id else None
    configured_execution_root = task.execution_workspace_root
    workspace_root_text = management_root if configured_execution_root is None else configured_execution_root
    invalid_conversation_reason = ""
    if requested_conversation_id and conversation is None:
        invalid_conversation_reason = "The heartbeat conversation no longer exists."
    if conversation is not None:
        bound_text = _conversation_execution_root(conversation)
        bound_root = Path(bound_text).expanduser() if bound_text else None
        if bound_root is None or (task.kind != "heartbeat" and workspace_root_text
                and main_worktree_root(bound_root) != main_worktree_root(Path(workspace_root_text).expanduser())):
            invalid_conversation_reason = "The heartbeat conversation belongs to another project."
            conversation = None
        elif getattr(conversation, "archived", False):
            invalid_conversation_reason = "The heartbeat conversation is archived."
            conversation = None
        else:
            workspace_root_text = bound_text
    if requested_conversation_id and conversation is None:
        return {
            "status": "failed",
            "workspace_root": management_root,
            "execution_workspace_root": workspace_root_text,
            "conversation_id": requested_conversation_id,
            "error": invalid_conversation_reason or "The heartbeat conversation is unavailable.",
        }
    if conversation is None and configured_execution_root == "":
        projectless_base = task.projectless_base
        if not projectless_base:
            raise ValueError("A projectless scheduled task requires its configured task directory.")
        conversation = repository.create_conversation(
            title=f"定时任务：{str(getattr(task, 'name', '') or '未命名任务')}", workspace_root="",
            projectless_base=projectless_base, initial_prompt=task.prompt,
            permission_mode=permission_mode, thread_source="automation",
        )
        workspace_root_text = conversation.execution_cwd
    workspace_root = Path(workspace_root_text).expanduser() if workspace_root_text else None
    if workspace_root is None or not workspace_root.exists() or not workspace_root.is_dir():
        return {
            "status": "failed", "workspace_root": management_root, "execution_workspace_root": workspace_root_text,
            "error": "Scheduled task workspace is unavailable. Open the project and update the task.",
        }
    run.execution_workspace_root = str(workspace_root.resolve())
    if conversation is None:
        conversation = repository.create_conversation(
            title=f"定时任务：{str(getattr(task, 'name', '') or '未命名任务')}",
            workspace_root=str(workspace_root.resolve()),
            git_branch=git_branch_for(workspace_root),
            permission_mode=permission_mode,
            thread_source="automation",
        )
    if str(getattr(run, "conversation_id", "") or "") != conversation.id:
        run.conversation_id = conversation.id
        if bind_conversation is not None:
            bind_conversation(run.id, conversation.id)

    query_guards = conversation_query_guards()
    manager = getattr(bootstrap, "ws_manager", None)
    if manager is not None and (manager._shutting_down or manager.conversation_delete_fence(conversation.id)):
        return {"status": "failed", "conversation_id": conversation.id,
            "workspace_root": management_root, "execution_workspace_root": run.execution_workspace_root,
            "error": "The heartbeat execution host is closing."}
    run_context = RunContext(turn_input_queue=TurnInputQueue())
    run_context.prepare_turn_owner()
    if run.start_receipt is not None:
        run_context.turn_started_receipt = run.start_receipt
    now = datetime.now(UTC).isoformat()
    from backend.services.turn_notifications import heartbeat_user_message

    command = ScheduledInputCommand(type="user_message", data={
        "content": heartbeat_user_message(task.id, now, task.prompt) if task.kind == "heartbeat" else task.prompt,
        "conversation_id": conversation.id, "user_message_id": f"schedule_{run.id}",
        "assistant_message_id": f"assistant_schedule_{run.id}",
    }, turn_trigger=run.turn_trigger or "", public_content=task.prompt,
        task_id=task.id, scheduled_run_id=run.id)
    if task.kind == "heartbeat":
        submission = query_guards.start_or_steer(
            conversation.id,
            owner_id=f"scheduled:{run.id}", owner=run_context, command=command,
            expected_admission_generation=run.host_admission_generation or None,
        )
    else:
        from backend.agent.conversation_query_guard import TurnInputSubmission

        claim = query_guards.try_start(conversation.id, owner_id=f"scheduled:{run.id}", owner=run_context)
        submission = TurnInputSubmission("started", claim=claim, owner=run_context) if claim is not None else TurnInputSubmission("not_submitted", reason="active_owner_unavailable")
    if submission.mode == "steered":
        owner = submission.owner
        if run.start_receipt is not None and not run.start_receipt.done():
            run.start_receipt.set_result({"status": "steered", "turn_id": owner.turn_input_queue.turn_id})
        run.actual_turn_id = owner.turn_input_queue.turn_id
        # Timeout/cancellation ends this scheduler wait, never the actual turn.
        try:
            async with asyncio.timeout(HEARTBEAT_TERMINAL_WAIT_SECONDS):
                result = await asyncio.shield(owner.turn_terminal_receipt)
                await owner.wait_for_turn_release()
        except TimeoutError:
            return {"status": "running", "conversation_id": conversation.id,
                "workspace_root": management_root, "execution_workspace_root": run.execution_workspace_root,
                "_turn_terminal_receipt": owner.turn_terminal_receipt,
                "_turn_owner": owner}
        return {
            "status": result["status"], "conversation_id": conversation.id,
            "workspace_root": management_root, "execution_workspace_root": run.execution_workspace_root, "summary": result["reply"],
            "message_id": result.get("assistant_message_id", ""),
            "actual_turn_id": result.get("turn_id", result["run_id"]),
            "error": result.get("error") or (result["reason"] if result["status"] != "completed" else ""),
        }
    query_claim = submission.claim
    if submission.mode == "not_submitted":
        return {
            "status": "failed",
            "workspace_root": management_root, "execution_workspace_root": run.execution_workspace_root,
            "conversation_id": conversation.id,
            "error": "The heartbeat conversation already has an active turn." if submission.reason == "active_owner_unavailable" else submission.reason,
        }
    interrupted = False
    try:
        runtime = default_runtime()
        run_context.agent_runtime = runtime
        run_context.execution_journal = runtime.execution_journal(execution_journal_owner(
            "conversation", repository.store_instance_id(), conversation.id,
        ))
        await replay_pending_conversation_projections(
            repository, run_context.execution_journal, conversation_id=conversation.id,
        )
        conversation = repository.get_conversation(conversation.id)
        if conversation is None:
            raise RuntimeError("The scheduled conversation disappeared before admission.")
        result = await _run_scheduled_task_owned(
            task,
            run,
            bootstrap=bootstrap,
            repository=repository,
            conversation=conversation,
            workspace_root=workspace_root,
            permission_mode=permission_mode,
            query_claim=query_claim,
            run_context=run_context,
        )
        return {**result, "workspace_root": management_root, "execution_workspace_root": run.execution_workspace_root}
    except asyncio.CancelledError:
        interrupted = True
        raise
    finally:
        # The scheduler's worker remains the resource owner after model/tool
        # cancellation. Its existing bounded stop operation can report pending
        # cleanup until these borrowers actually finish.
        cancelled_during_cleanup = False
        while pending := {task for task in run_context.lifecycle_cleanup_tasks if not task.done()}:
            try:
                await asyncio.wait(pending)
            except asyncio.CancelledError:
                cancelled_during_cleanup = True
        try:
            if interrupted and query_guards.owns(query_claim):
                await replay_pending_conversation_projections(
                    repository, run_context.execution_journal, conversation_id=conversation.id,
                )
        finally:
            if run_context.close_turn_inputs is not None:
                run_context.close_turn_inputs()
            query_guards.end(query_claim)
        if cancelled_during_cleanup:
            raise asyncio.CancelledError


async def _run_scheduled_task_owned(
    task: Any,
    run: Any,
    *,
    bootstrap: Any,
    repository: ConversationRepository,
    conversation: Any,
    workspace_root: Path,
    permission_mode: str,
    query_claim: ConversationQueryClaim,
    run_context: RunContext,
) -> dict[str, Any]:
    execution_root = workspace_root.resolve()
    isolation = str(getattr(task, "isolation", "worktree") or "worktree").strip().lower()
    if isolation == "worktree" and task.execution_workspace_root != "":
        existing_worktree_text = str(getattr(conversation, "worktree_path", "") or "").strip()
        existing_worktree = Path(existing_worktree_text).expanduser() if existing_worktree_text else None
        existing_matches_project = False
        if existing_worktree is not None and existing_worktree.is_dir():
            try:
                existing_matches_project = main_worktree_root(existing_worktree) == main_worktree_root(workspace_root)
            except (OSError, RuntimeError, ValueError):
                existing_matches_project = False
        if getattr(conversation, "git_isolated", False) and existing_matches_project and existing_worktree is not None:
            execution_root = existing_worktree.resolve()
        else:
            from backend.services.conversation_payload_service import create_isolated_worktree_binding

            def create_and_bind_worktree():
                creation = create_isolated_worktree_binding(
                    conversation,
                    current_workspace_root=workspace_root,
                    main_worktree_root=main_worktree_root,
                )
                updated = repository.update_workspace_binding(
                    conversation.id,
                    workspace_root=creation.workspace_root,
                    git_branch=creation.git_branch,
                    worktree_path=creation.worktree_path,
                    git_isolated=True,
                ) if creation.created else None
                return creation, updated

            # Creation and its durable binding share one cancellation boundary.
            creation, updated = await to_thread_cancel_safe(create_and_bind_worktree)
            if not creation.created:
                error = getattr(creation.error_event, "data", {}).get("message") if creation.error_event else ""
                return {
                    "status": "failed",
                    "workspace_root": str(workspace_root.resolve()),
                    "conversation_id": conversation.id,
                    "error": str(error or "Unable to create an isolated worktree for the scheduled task."),
                }
            if updated is None:
                return {
                    "status": "failed",
                    "workspace_root": str(workspace_root.resolve()),
                    "conversation_id": conversation.id,
                    "error": "The scheduled worktree was created but the conversation binding could not be saved.",
                }
            conversation = updated
            execution_root = Path(creation.workspace_root).resolve()
    run.execution_workspace_root = str(execution_root)

    from backend.agent.plans import merge_plan_constraints, plan_path_for_snapshot
    from backend.permissions.checker import clamp_permission_mode
    from backend.permissions.context import PermissionContext
    from backend.permissions.profiles import workspace_scope_for
    from backend.tools.base import PermissionLevel

    def live_permission_context() -> PermissionContext:
        owner = repository.get_conversation(conversation.id)
        if owner is None:
            raise RuntimeError("The scheduled conversation no longer exists.")
        mode = clamp_permission_mode(permission_mode, owner.permission_mode)
        plan_path = (
            plan_path_for_snapshot(owner.context_snapshot, execution_root)
            if mode == "plan" else None
        )
        return PermissionContext(
            mode=mode,
            tool_deny_rules=owner.permission_deny_rules,
            session_overrides={name: PermissionLevel(level) for name, level in owner.permission_overrides.items()},
            filesystem_constraints=merge_plan_constraints({}, plan_path),
            workspace_scope=workspace_scope_for(workspace_root=owner.workspace_root, worktree_path=owner.worktree_path),
            conversation_id=owner.id, workspace_root=execution_root, source="scheduled_task",
            pre_plan_mode=(
                clamp_permission_mode(owner.permission_previous_mode, permission_mode)
                if mode == "plan" and owner.permission_previous_mode else None
            ),
        )

    run_context.conversation_repository = repository
    run_context.permission_context_provider = live_permission_context
    now = datetime.now(UTC).isoformat()
    snapshot = dict(conversation.context_snapshot or {})
    if not snapshot.get("history") and conversation.transcript:
        # Older detached runs persisted only display messages. Migrate their
        # actual text once; activity rows are not a substitute for tool results.
        snapshot["history"] = [
            {"role": item["role"], "content": item["content"]}
            for item in conversation.transcript
            if item.get("role") in {"user", "assistant"}
            and isinstance(item.get("content"), str)
            and item["content"]
        ]
    admitted = repository.append_transcript_message(
        conversation.id,
        {
            "id": f"schedule_{getattr(run, 'id', '')}",
            "role": "user",
            "content": str(getattr(task, "prompt", "") or ""),
            "timestamp": now,
            "metadata": {
                "source": "scheduled_task",
                "scheduled_task_id": str(getattr(task, "id", "")),
                "scheduled_run_id": str(getattr(run, "id", "")),
            },
        },
    )
    if admitted is None:
        raise RuntimeError("The scheduled conversation disappeared before its prompt was saved.")
    run_context.conversation_revision = admitted.revision

    from backend.services.turn_notifications import heartbeat_user_message

    query_message = heartbeat_user_message(task.id, now, task.prompt) if task.kind == "heartbeat" else task.prompt
    result = await run_owned_rest_chat(
        message=query_message,
        max_iterations=None,
        bootstrap=bootstrap,
        query_engine=None,
        workspace_root=execution_root,
        permission_mode=permission_mode,
        conversation_id=conversation.id,
        run_id=str(getattr(run, "id", "")),
        query_claim=query_claim,
        run_context=run_context,
        conversation_snapshot=snapshot,
        automation_kind=task.kind,
        notification_policy=task.notification_policy,
        turn_trigger=run.turn_trigger,
        model_selection=({key: value.strip() for key, value in {
            "provider": task.provider, "model": task.model, "reasoning_effort": task.reasoning_effort,
        }.items() if value is not None and value.strip()} if task.kind != "heartbeat" else None),
    )
    if not conversation_query_guards().owns(query_claim):
        return {
            "status": "cancelled",
            "conversation_id": conversation.id,
            "workspace_root": str(workspace_root.resolve()),
            "execution_workspace_root": str(execution_root),
            "summary": "",
            "error": "The heartbeat conversation query generation changed before terminal commit.",
        }
    stopped_reason = str(result.get("stopped_reason") or "completed")
    reported_status = str(result.get("status") or "").strip().lower()
    status = (
        reported_status
        if reported_status in {"completed", "partial", "failed", "cancelled"}
        else "completed"
        if stopped_reason == "completed"
        else "failed"
    )
    reply = str(result.get("reply") or "")
    from backend.services.turn_notifications import completion_notification

    notification = completion_notification(
        status=status, last_agent_message=reply, thread_source=conversation.thread_source,
        automation_kind=task.kind, notification_policy=task.notification_policy,
    )
    heartbeat_message = notification["heartbeat_assistant_message"]
    visible_reply = heartbeat_message["visible_text"] if heartbeat_message is not None else reply
    errors = [str(item) for item in result.get("errors", []) if str(item).strip()]
    assistant_message = {
        "id": f"assistant_schedule_{getattr(run, 'id', '')}",
        "role": "assistant",
        "content": visible_reply,
        "timestamp": datetime.now(UTC).isoformat(),
        "terminal_status": status,
        "termination_reason": stopped_reason,
        **({"failure_message": errors[-1]} if status == "failed" and errors else {}),
        "metadata": {
            "source": "scheduled_task",
            "scheduled_task_id": str(getattr(task, "id", "")),
            "scheduled_run_id": str(getattr(run, "id", "")),
            "stopped_reason": stopped_reason,
            "iterations": int(result.get("iterations") or 0),
            "errors": errors,
            "completion_notification": notification,
        },
    }
    next_snapshot = {
        **snapshot,
        **result.get("context_snapshot", {}),
        "scheduled_task": {
            "task_id": str(getattr(task, "id", "")),
            "run_id": str(getattr(run, "id", "")),
            "status": status,
            "stopped_reason": stopped_reason,
            **run_context.lifecycle_cleanup_evidence(),
        }
    }
    journal = run_context.execution_journal
    from backend.conversations.context_delta import context_snapshot_delta
    pending_projection = journal.append_lifecycle(
        "conversation_projection_pending",
        {
            "conversation_id": conversation.id, "assistant_message": assistant_message,
            "context_snapshot": next_snapshot, "expected_revision": run_context.conversation_revision,
            "context_delta": context_snapshot_delta(snapshot, next_snapshot),
            "run_id": str(getattr(run, "id", "")),
            "source_user_message_ids": [f"schedule_{getattr(run, 'id', '')}"],
        },
    ) if journal is not None else None
    committed = repository.commit_turn_projection(
        conversation.id,
        assistant_message=assistant_message,
        context_snapshot=next_snapshot,
        expected_revision=run_context.conversation_revision,
    )
    if committed is None:
        raise RuntimeError("The scheduled conversation disappeared before its result was saved.")
    if pending_projection is not None:
        journal.append_lifecycle(
            "conversation_projection_committed",
            {
                "conversation_id": conversation.id, "message_id": assistant_message["id"],
                "pending_event_id": pending_projection.event_id, "conversation_revision": committed.revision,
                "run_id": str(getattr(run, "id", "")),
            },
        )
    return {
        "status": status,
        "conversation_id": conversation.id,
        # History remains grouped under the configured project even when the
        # actual run happens in a nested managed worktree.
        "workspace_root": str(workspace_root.resolve()),
        "execution_workspace_root": str(execution_root),
        "summary": visible_reply,
        "completion_notification": notification,
        "message_id": assistant_message["id"],
        "actual_turn_id": str(getattr(run, "id", "")),
        **run_context.lifecycle_cleanup_evidence(),
        "error": stopped_reason if status in {"failed", "cancelled"} else "",
    }
