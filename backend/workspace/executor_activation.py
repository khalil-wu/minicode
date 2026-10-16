"""New conversation ownership for a registered original-executor worktree."""
from __future__ import annotations

from dataclasses import dataclass, replace

from backend.agent.environment_runtime import resolve_environment_config, thread_environment_defaults
from backend.agent.environment_selection import ThreadEnvironments, TurnEnvironment, select_turn_environment
from backend.environments.errors import ExecutorProtocolError
from backend.services.workspace_api_service import capture_workspace_execution
from backend.workspace.executor_editor import ExecutorEditorService
from backend.workspace.executor_worktree import executor_worktree_action
from backend.workspace.executor_paths import ExecutorPath


@dataclass
class ExecutorWorkspaceActivation:
    attachment: TurnEnvironment
    path_uri: str
    branch: str

    async def bind(self, session, conversation):
        permission = session.permission_context_for_conversation(conversation, source="workspace.executor.activate")
        owner = ThreadEnvironments.retarget(session.environment_manager,
            thread_environment_defaults(permission, session.config.config_layer_stack), resolve_config=resolve_environment_config,
            attachment=self.attachment, cwd=self.path_uri, workspace_roots=(self.path_uri,))
        accepted = False
        try:
            snapshot = await owner.snapshot()
            try:
                attached = select_turn_environment(snapshot, self.attachment.selection.environment_id)
                if ExecutorPath.parse(attached.selection.cwd) != ExecutorPath.parse(self.path_uri):
                    raise ExecutorProtocolError("The new conversation did not accept the target worktree")
            finally:
                await snapshot.aclose()
            runtime = session.ws_manager.execution_contexts.thread_runtime(session.conversation_repo.store_instance_id(), conversation.id)
            async with runtime.environment_lock:
                if runtime.thread_environments is not None:
                    existing = await runtime.thread_environments.snapshot()
                    try:
                        accepted_attachment = select_turn_environment(existing, self.attachment.selection.environment_id)
                        if (accepted_attachment.environment is not self.attachment.environment
                                or ExecutorPath.parse(accepted_attachment.selection.cwd) != ExecutorPath.parse(self.path_uri)):
                            raise ExecutorProtocolError("The conversation already owns a different executor workspace")
                    finally:
                        await existing.aclose()
                    return
                runtime.thread_environments = owner
                accepted = True
        finally:
            if not accepted:
                await owner.aclose()
            await self.attachment.lease.aclose()

    async def aclose(self):
        await self.attachment.lease.aclose()


async def prepare_executor_workspace_activation(session, target_uri):
    source = session.active_conversation_id
    if not source:
        raise ExecutorProtocolError("An active executor conversation is required to open its worktree")
    async with capture_workspace_execution(session, source) as context:
        if context is None or not context.turn_environment.is_remote:
            raise ExecutorProtocolError("The active conversation has no remote execution attachment")
        async with ExecutorEditorService(context, application_config=session.config) as service:
            payload = await executor_worktree_action(service, "switch", path=target_uri)
            project = payload["project"]
            return ExecutorWorkspaceActivation(replace(context.turn_environment,
                lease=context.turn_environment.lease.clone()), project["path_uri"], project["current_branch"])


async def validate_executor_workspace(session, conversation):
    async with capture_workspace_execution(session, conversation.id) as context:
        if context is None:
            raise ExecutorProtocolError("The conversation has no execution attachment")
        async with ExecutorEditorService(context, application_config=session.config) as service:
            path = ExecutorPath.parse(conversation.execution_cwd or conversation.worktree_path or conversation.workspace_root)
            if service.cwd != path:
                raise ExecutorProtocolError("Workspace execution owner is stale")
            canonical = await service.filesystem.canonicalize(path.uri, sandbox=service.sandbox)
            if service.resolve(canonical) != path:
                raise ExecutorProtocolError("Workspace canonical path changed")
            info = await service.metadata(path)
            if info is None or not info["isDirectory"]:
                raise ExecutorProtocolError("The executor worktree directory is unavailable")
