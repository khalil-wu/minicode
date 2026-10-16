"""Private Guardian sessions use the existing QueryEngine and real OS sandbox."""
from __future__ import annotations

import base64
import json
from io import BytesIO
from dataclasses import dataclass, replace
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend.agent.context import ContextBuilder, clone_context_builder
from backend.agent.guardian.contract import GuardianReviewError
from backend.agent.guardian.config import reviewer_permission_profile
from backend.agent.guardian.context import GuardianCheckpointRecoveryNeeded, GuardianParentCheckpoint
from backend.agent.guardian.prompt import GuardianPrompt, FOLLOWUP_REMINDER
from backend.agent.loop_session import AgentLoopSessionContext
from backend.agent.model_execution import ModelExecutionSnapshot
from backend.agent.run_context import RunContext
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.hooks.manager import HookManager
from backend.llm.base import LLMMessage, estimate_llm_context_tokens
from backend.media_types import media_type_for_path
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, PermissionDecision
from backend.services.llm_adapter_factory import create_session_llm
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.code_execution import ToolExecTool, ToolWaitTool
from backend.tools.command_tool import RunCommandTool
from backend.tools.monitor_tool import MonitorTool
from backend.tools.registry import ToolRegistry

REVIEWER_TOOLS = frozenset({"run_command", "monitor", "view_image", "tool_exec", "tool_wait"})


@dataclass
class GuardianInputMessage(LLMMessage):
    guardian_parts: tuple[str, ...] = ()

    def user_text_parts(self) -> list[str]:
        return list(self.guardian_parts)


def guardian_input_messages(items):
    """Keep native inter-agent evidence between ordered user text blocks."""
    messages = []
    text_parts = []
    for item in items:
        if isinstance(item, str):
            text_parts.append(item)
            continue
        if text_parts:
            messages.append(GuardianInputMessage(role="user", content="\n".join(text_parts), guardian_parts=tuple(text_parts)))
            text_parts = []
        if item.get("type") == "input_image":
            prefix, data = item["image_url"].split(";base64,", 1)
            messages.append(LLMMessage(role="user", content="", images=[{"media_type": prefix.removeprefix("data:"), "data": data,
                **({"detail": item["detail"]} if item.get("detail") is not None else {})}]))
            continue
        messages.append(LLMMessage(role="assistant", provider_items=[deepcopy(item)]))
    if text_parts:
        messages.append(GuardianInputMessage(role="user", content="\n".join(text_parts), guardian_parts=tuple(text_parts)))
    return messages


class GuardianContextBuilder(ContextBuilder):
    def __init__(self, *, policy: str, **kwargs):
        super().__init__(include_skill_instructions=False, **kwargs)
        self.policy = policy
        self.pending_parts: tuple[str | dict[str, Any], ...] = ()
        self.followup_reminder = False
        self.parent_checkpoint: GuardianParentCheckpoint | None = None
        self.checkpoint_recovery_requested = False
        self.fresh_checkpoint_input = False
        self.reviewer_tool_schemas: list[dict[str, Any]] = []
        self.last_admitted_node_repl_response_sequence = 0
        self.pending_node_repl_evidence_admission: tuple[str, int] | None = None

    def base_system_prompt(self, state) -> str:
        return self.policy

    async def prepare_system_prompt(self, state) -> str:
        return self.policy

    async def _ensure_environment_project_instructions(self) -> None:
        # The reviewer receives the issuer's instruction evidence in its action.
        return

    def _get_project_guidelines(self, *args, **kwargs) -> str:
        # The issuing host supplies applied instruction evidence, and Source
        # Guardian does not load a second Skill/plugin/parent prompt bundle.
        return ""

    async def start_turn(self, user_message: str, state) -> LLMMessage:
        messages = guardian_input_messages(self.pending_parts or (user_message,))
        for message in messages:
            self._history_store.append(message, raw_content=message.content)
        self.pending_parts = ()
        return messages[-1]

    def admit_node_repl_evidence(self, *, boundary_input, history_start: int, history_end: int) -> None:
        # QueryEngine invokes its actual admission committer after the complete
        # user history/evidence append succeeds. A rolled-back start_turn must
        # not move this cursor merely because messages were briefly prepared.
        pending = self.pending_node_repl_evidence_admission
        context = self._tool_execution_context
        if pending is not None and context is not None and pending[0] == context.turn_id and any(
            isinstance(message, GuardianInputMessage) and any(part in {">>> TRANSCRIPT START\n", ">>> TRANSCRIPT DELTA START\n"}
                for part in message.guardian_parts) for message in self._history[history_start:history_end]):
            self.last_admitted_node_repl_response_sequence = max(self.last_admitted_node_repl_response_sequence, pending[1])
            self.pending_node_repl_evidence_admission = None

    def _render_prompt_messages(self, state, workspace_root=None, prompt_parts=None):
        prefix = [LLMMessage(role="developer", content=self.policy)]
        if self.followup_reminder:
            prefix.append(LLMMessage(role="developer", content=FOLLOWUP_REMINDER))
        return [*prefix, *self._history_store.messages]

    async def build(self, user_message, state=None, *, allow_time_based_microcompact=True):
        if not isinstance(user_message, AgentState):
            await self.start_turn(user_message, state)
        self._history_store.ensure_timestamps()
        active = user_message if isinstance(user_message, AgentState) else state
        return self._render_prompt_messages(active)

    @property
    def token_usage(self) -> int:
        return estimate_llm_context_tokens(self._render_prompt_messages(None))

    def needs_compaction(self, *args, **kwargs) -> bool:
        # Source skips startup compaction for one fresh captured checkpoint;
        # final admission must fit the full action around that fixed state.
        if self.parent_checkpoint is not None and self.fresh_checkpoint_input:
            return False
        return super().needs_compaction(*args, **kwargs)

    async def compact(self, *args, **kwargs):
        if self.parent_checkpoint is not None:
            self.checkpoint_recovery_requested = True
            raise GuardianCheckpointRecoveryNeeded()
        # Without a parent checkpoint Source may compact this reviewer's own
        # real provider history; the fixed policy remains the startup prefix.
        return await super().compact(*args, **kwargs)


class GuardianPermissionChecker(PermissionChecker):
    def with_workspace_root(self, workspace_root):
        return type(self)(self._settings, workspace_root)

    def capability_available(self, tool_name, *, context=None, tool=None):
        allowed = tool_name in REVIEWER_TOOLS
        return allowed, "" if allowed else "Tool is outside Source Guardian's reviewer tool policy"

    def evaluate(self, tool_name, args=None, *, context=None, tool=None):
        allowed, reason = self.capability_available(tool_name, context=context, tool=tool)
        return PermissionDecision(PermissionLevel.AUTO if allowed else PermissionLevel.ALWAYS_DENY,
            "allow" if allowed else "deny", allowed, reason, "auto" if allowed else "deny",
            "guardian_reviewer", "source_read_only_tool_policy", "low", {"workspace_scope": "project"}, "call")


class GuardianCommandTool(RunCommandTool):
    permission = PermissionLevel.AUTO

    def __init__(self, artifacts, sandbox_policy):
        super().__init__(artifacts)
        self.review_sandbox_policy = sandbox_policy

    def check_permission(self, args=None, context=None):
        return PermissionLevel.AUTO

    def capture_exec_policy(self, request, context, permission):
        if request.argument_values().get("sandbox_permissions", "use_default") != "use_default":
            raise ValueError("Guardian commands cannot request sandbox escalation")
        scope = permission.environment_scope
        if scope is not None:
            scope = replace(scope, permission_profile=reviewer_permission_profile(scope.permission_profile))
            cwd = scope.resolve_path(request.argument_values().get("cwd") or scope.cwd_uri)
            if not scope.resolve_access(cwd.uri).can_read:
                raise ValueError("Guardian's read-only policy denies the requested working directory")
        readonly = replace(permission, mode="confirm", approval_policy="never", sandbox_mode="read-only",
            allow_unsandboxed_commands=False, additional_permissions=None, environment_scope=scope)
        return super().capture_exec_policy(request, replace(context, permission=readonly,
            sandbox_policy=self.review_sandbox_policy, allow_network=False), readonly)

    def _resolve_cwd(self, cwd, context, *, allow_workspace_escape=False):
        resolved = super()._resolve_cwd(cwd, context, allow_workspace_escape=True)
        if context.turn_environment is None and resolved is not None and not self.review_sandbox_policy.resolve(cwd=resolved).resolve_access(resolved).can_read:
            raise ValueError("Guardian's read-only policy denies the requested working directory")
        return resolved

    async def execute(self, args, context=None):
        if args.get("with_escalated_permissions") is True:
            return self._error_result("Guardian commands cannot request sandbox escalation")
        readonly = replace(context, sandbox_policy=self.review_sandbox_policy, allow_network=False,
            permission=replace(context.permission, mode="confirm", approval_policy="never", sandbox_mode="read-only", allow_unsandboxed_commands=False))
        return await super().execute(args, readonly)


class GuardianViewImageTool(BaseTool):
    name = "view_image"
    permission = PermissionLevel.AUTO
    read_only = True
    requires_execution_environment = True
    workspace_path_fields = ("path",)
    description = "View a local image file from the filesystem when visual inspection is needed."

    def __init__(self, sandbox_policy):
        self.review_sandbox_policy = sandbox_policy

    def get_schema(self):
        return ToolSchema(self.name, self.description, {"type": "object", "additionalProperties": False,
            "properties": {"path": {"type": "string"}, "detail": {"type": "string", "enum": ["high", "original"]}}, "required": ["path"]})

    async def execute(self, args, context=None):
        from backend.async_cleanup import to_thread_cancel_safe
        from PIL import Image

        from backend.workspace.executor_service import ExecutorWorkspaceService, uses_executor_io

        if uses_executor_io(context):
            scope = context.permission.environment_scope
            readonly_scope = replace(scope, permission_profile=reviewer_permission_profile(scope.permission_profile))
            readonly = replace(context, permission=replace(context.permission, environment_scope=readonly_scope))
            async with ExecutorWorkspaceService(readonly) as service:
                path = service.resolve(args["path"])
                if not readonly_scope.resolve_access(path.uri).can_read:
                    return ToolResult("Guardian's read-only policy denies this image path", is_error=True)
                image_bytes = await service.read_bytes(path)
                display, image_type = path.native, media_type_for_path(path.name)
        else:
            path = Path(args["path"]).expanduser()
            path = path if path.is_absolute() else context.workspace_root / path
            if not self.review_sandbox_policy.resolve(cwd=context.workspace_root).resolve_access(path).can_read:
                return ToolResult("Guardian's read-only policy denies this image path", is_error=True)
            image_bytes = await to_thread_cancel_safe(path.read_bytes)
            display, image_type = str(path), media_type_for_path(path)
        def verify_image():
            with Image.open(BytesIO(image_bytes)) as loaded:
                loaded.verify()
        await to_thread_cancel_safe(verify_image)
        return ToolResult(content=f"Loaded image: {display}", images=[{"data": base64.b64encode(image_bytes).decode("ascii"), "media_type": image_type}])


class GuardianRuntime:
    """One standard harness session, shared only by its Source reviewer pool."""
    def __init__(self, *, parent_snapshot, config, policy: str, sandbox_policy, artifacts: ArtifactStore, conversation_id: str,
        seed_context: GuardianContextBuilder | None = None,
        parent_checkpoint: GuardianParentCheckpoint | None = None, llm_factory=create_session_llm):
        from backend.agent.query_engine import AgentSession

        self.config = config
        self.sandbox_policy = sandbox_policy
        self.workspace_root = sandbox_policy.workspace_root if sandbox_policy is not None else None
        self.id = f"guardian_{uuid4().hex}"
        self.conversation_id = conversation_id
        self.llm = llm_factory(config, model_override=config.llm.model,
            provider_override=parent_snapshot.provider, model_runtime=parent_snapshot.model_runtime)
        self.model_execution = ModelExecutionSnapshot.capture(config, self.llm)
        self.context_builder = (clone_context_builder(seed_context) if seed_context is not None else GuardianContextBuilder(policy=policy,
            token_budget=config.token_budget, agent_settings=config.agent, llm=self.llm,
            workspace_root=self.workspace_root, conversation_id=self.id))
        self.context_builder.bind_llm(self.llm)
        self.context_builder.parent_checkpoint = parent_checkpoint
        if seed_context is None and parent_checkpoint is not None:
            checkpoint_message = parent_checkpoint.model_message()
            self.llm.validate_context([checkpoint_message])
            self.context_builder._history_store.append(checkpoint_message)
        registry = ToolRegistry()
        registry.register(GuardianCommandTool(artifacts, sandbox_policy))
        registry.register(MonitorTool())
        registry.register(GuardianViewImageTool(sandbox_policy))
        registry.register(ToolExecTool())
        registry.register(ToolWaitTool())
        self.context_builder.reviewer_tool_schemas = registry.get_schemas()
        checker = GuardianPermissionChecker(config.permissions, self.workspace_root)
        self.session = AgentSession(llm=self.llm, tool_registry=registry, artifact_store=artifacts,
            permission_checker=checker, agent_settings=config.agent, token_budget=config.token_budget,
            context_builder=self.context_builder)
        self.completed_reviews = 0
        self.closed = False

    def committed_context(self):
        return clone_context_builder(self.context_builder)

    async def run(self, prompt: GuardianPrompt, *, cancel_event, parent_context, deadline: float) -> str:
        from backend.agent.query_engine import QueryEngine, QuerySubmission

        self.context_builder.pending_parts = prompt.items
        self.context_builder.checkpoint_recovery_requested = False
        self.context_builder.fresh_checkpoint_input = self.completed_reviews == 0 and self.context_builder.parent_checkpoint is not None
        self.context_builder.followup_reminder = self.completed_reviews > 0
        owner = RunContext(cancel_event=cancel_event, model_execution=self.model_execution,
            active_model_execution=self.model_execution, hook_manager=HookManager(),
            retain_model=parent_context.run_context.retain_model if parent_context.run_context is not None else None)
        parent_owner = parent_context.run_context
        if parent_owner is not None:
            owner.environment_manager = parent_owner.environment_manager
            owner.environment_manager_lease = parent_owner.environment_manager_lease
            owner.user_shell = dict(parent_owner.user_shell)
            owner.environment_host_mcp_manager = parent_owner.environment_host_mcp_manager
            owner.environment_host_skill_manager = parent_owner.environment_host_skill_manager
            owner.environment_host_captured = parent_owner.environment_host_captured
            if self.session.thread_environments is None and parent_owner.turn_environments is not None:
                owner.inherited_turn_environments = parent_owner.turn_environments.clone()
                from backend.agent.environment_selection import EnvironmentConfigState, FailedTurnEnvironment, TurnEnvironment

                owner.environment_requests = tuple(
                    replace(environment.selection, config=EnvironmentConfigState.ready(replace(environment.config,
                        permission_profile=reviewer_permission_profile(environment.config.permission_profile)))).into_request()
                    if isinstance(environment, TurnEnvironment)
                    else selection.into_request()
                    for environment, selection in zip(parent_owner.turn_environments.environments,
                        parent_owner.turn_environments.all_selections())
                    if not isinstance(environment, FailedTurnEnvironment))
        owner.session_source = "guardian"
        owner.is_subagent = True
        owner.isolated = True
        permission = PermissionContext(mode="confirm", approval_policy="never", sandbox_mode="read-only",
            conversation_id=self.id, workspace_root=self.workspace_root,
            allow_unsandboxed_commands=False, sandbox_auto_allow_commands=True)
        runtime = AgentLoopSessionContext(workspace_root=self.workspace_root, session_id=self.id,
            run_context=owner, permission_context=permission, cancel_event=cancel_event)
        state = AgentState(user_message=prompt.text, conversation_id=self.id, workspace_root=self.workspace_root)
        metadata = {"query_source": "guardian_review", "conversation_id": self.id, "run_id": f"guardian_review_{uuid4().hex}",
            "parent_turn_id": parent_context.turn_id, "parent_response_id": parent_context.metadata.get("parent_response_id"),
            "internal_session_source": "guardian",
            "_source_responses_metadata": {"thread_source": "guardian_review", "turn_trigger": "guardian_review",
                "parent_thread_id": self.conversation_id, "parent_turn_id": parent_context.turn_id,
                **({"root_turn_id": parent_context.metadata["root_turn_id"]} if parent_context.metadata.get("root_turn_id") is not None else {})},
            "system_prompt": self.context_builder.policy, "prompt_cache_key": f"guardian:{self.conversation_id}"}
        runtime.metadata = metadata
        metadata["commit_turn_admission"] = self.context_builder.admit_node_repl_evidence
        self.context_builder.pending_node_repl_evidence_admission = (
            (metadata["run_id"], prompt.node_repl_evidence_sequence)
            if prompt.node_repl_evidence_sequence > self.context_builder.last_admitted_node_repl_response_sequence else None)
        terminal = None
        reply = ""
        async for event in QueryEngine().submit(QuerySubmission(user_message=prompt.text, session=self.session, state=state, runtime=runtime)):
            if event.type == "agent_message" and event.data.get("phase") != "commentary":
                reply = str(event.data.get("content") or reply)
            if event.type == "done":
                terminal = event.data
        if self.context_builder.checkpoint_recovery_requested:
            raise GuardianCheckpointRecoveryNeeded()
        if terminal is None or terminal.get("status") not in {"completed", "success"}:
            if cancel_event.is_set():
                raise GuardianReviewError("cancelled")
            raise GuardianReviewError("session", str((terminal or {}).get("reason") or "guardian review did not complete"))
        reply = state.reply or reply
        if not reply:
            raise GuardianReviewError("session", "guardian review completed without an assessment payload")
        self.completed_reviews += 1
        return reply

    async def aclose(self):
        if self.closed:
            return
        self.closed = True
        await self.session.aclose()
        await self.llm.aclose()
