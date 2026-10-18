"""Create a separate review thread on the source's accepted execution owner.

Source review/start recommends thread/start followed by an inline review.
The original user_message keeps its durable owner; a separate thread receives
its own normally admitted input instead of reassigning the parent's message.
"""
from __future__ import annotations

from dataclasses import replace

from backend.services.code_review_service import prepare_code_review_on_executor
from backend.services.git_settings_service import get_git_preferences
from backend.ws.command_scope import CommandScope


async def create_review_context(session, owner: CommandScope, data, *, delivery: str | None = None):
    from backend.ws.handlers.diff import _git_execution
    from backend.ws.handlers.conversation import _broadcast_conversation_lists
    from backend.workspace.executor_activation import ExecutorWorkspaceActivation

    source = session.conversation_repo.get_conversation(owner.conversation_id)
    if source is None or source.archived:
        raise ValueError("Select an available conversation before starting code review.")
    selected_delivery = get_git_preferences().review_delivery if delivery is None else delivery
    identity = {}
    async with _git_execution(session, owner, data, identity) as service:
        prepared = await prepare_code_review_on_executor(owner.workspace_root, service, data["mode"],
            data.get("base_branch", ""), data.get("merge_base_sha", ""), data.get("request_message", ""))
        review = source
        if selected_delivery == "detached":
            review = session.conversation_repo.create_conversation(
                title=f"Review: {source.title}", thread_source="review", conversation_type="main",
                permission_mode=source.permission_mode, agent_mode=source.agent_mode,
                permission_deny_rules=list(source.permission_deny_rules), permission_overrides=dict(source.permission_overrides),
                model_selection=dict(source.model_selection), memory_mode=source.memory_mode,
                workspace_root=source.workspace_root, execution_cwd=prepared["scope"]["cwd"],
                git_branch=source.git_branch, git_isolated=False, worktree_path="",
                parent_conversation_id=source.id, branch_kind="review", initial_prompt=prepared["requestMessage"],
                creation_command_id=(f"review:{source.id}:{session.session_id}:{owner.request_id}" if owner.request_id else ""),
            )
            if service is not None:
                attachment = service.context.turn_environment
                activation = ExecutorWorkspaceActivation(replace(attachment, lease=attachment.lease.clone()),
                    service.cwd.uri, source.git_branch)
                await activation.bind(session, review)
    if selected_delivery == "detached":
        await _broadcast_conversation_lists(session)
    return {**prepared, "delivery": selected_delivery, "reviewThreadId": review.id, "sourceConversationId": source.id}


async def queue_detached_review(session, source_conversation_id: str, scope, *, prompt: str, request_message: str, input_id: str):
    """A native /review starts a distinct, durable user input on its new thread."""
    import hashlib
    from backend.agent.message import UserCommand

    source = session.conversation_repo.get_conversation(source_conversation_id)
    directory = source.execution_cwd or source.worktree_path or source.workspace_root
    owner = CommandScope(source.id, directory, input_id)
    prepared = await create_review_context(session, owner, {
        "mode": scope["mode"], "base_branch": scope.get("baseBranch", ""),
        "merge_base_sha": scope.get("mergeBaseSha", ""),
        "request_message": request_message,
    }, delivery="detached")
    identity = hashlib.sha256(f"{session.session_id}\0{input_id}\0{prepared['reviewThreadId']}".encode()).hexdigest()
    command = UserCommand("user_message", {
        "client_command_id": "review_" + identity, "conversation_id": prepared["reviewThreadId"],
        "content": prompt, "display_content": request_message,
        "code_review": prepared["scope"], "workspace_root": prepared["scope"]["cwd"],
        "user_message_id": "user_review_" + identity, "assistant_message_id": "assistant_review_" + identity,
    })
    queue = session.run_manager.durable_client_commands
    if not queue.persist_client_command(command):
        raise RuntimeError("The separate review input could not be persisted.")
    session.command_dispatcher._schedule_durable_client_command(command.data["client_command_id"],
        session.connection_generation, command_type="user_message")
    return prepared["reviewThreadId"]
