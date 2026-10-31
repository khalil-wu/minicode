from __future__ import annotations

from dataclasses import replace
from typing import Any

from backend.agent.message import AgentEvent
from backend.conversations.models import DEFAULT_CONVERSATION_PERMISSION_MODE
from backend.permissions.profiles import workspace_scope_for
from backend.agent.plans import merge_plan_constraints, plan_path_for_snapshot
from backend.tools.base import PermissionLevel
from backend.permissions.approval_policy import ApprovalPolicy, policy_for_mode
from backend.ws.utils import (
    normalize_permission_mode,
    normalize_permission_overrides,
    normalize_tool_patterns,
    permission_level_to_token,
)


def _managed_permission_projection(mode: str, configured_policy: ApprovalPolicy | None = None, *, config_stack: Any = None) -> tuple[str, Any, str, str]:
    from backend.config import get_config_requirements

    requirements = config_stack.requirements if config_stack is not None else get_config_requirements()
    policy = policy_for_mode(mode, configured_policy)
    effective_config = config_stack.effective_config() if config_stack is not None else {}
    configured_sandbox = effective_config.get("sandbox_mode", "") if mode not in {"plan", "bypass"} else ""
    effective_mode, violation = requirements.resolve_permission_mode(mode, approval_policy=policy, sandbox_mode=configured_sandbox)
    if violation is not None:
        raise violation
    requirement_source = requirements.source_for("allowed_approval_policies") or requirements.source_for(
        "allowed_sandbox_modes"
    )
    return (
        effective_mode,
        policy.to_wire(),
        requirements.sandbox_mode_for_permission_mode(effective_mode, sandbox_mode=configured_sandbox),
        str(requirement_source or ""),
    )


class SessionPermissionRuntimeMixin:
    def _command_prompt_rules_for_conversation(self, conversation_id: str) -> tuple[str, ...]:
        owner = str(conversation_id or "").strip()
        if not owner or self.ws_manager is None:
            return self._local_command_prompt_allow_rules.get(owner, ())
        context = self.ws_manager.execution_contexts.context_for(owner)
        return context.thread_runtime.command_prompt_allow_rules if context is not None else ()

    def _set_command_prompt_rules_for_conversation(self, conversation_id: str, rules: tuple[str, ...]) -> None:
        owner = str(conversation_id or "").strip()
        if not owner or self.ws_manager is None:
            self._local_command_prompt_allow_rules[owner] = rules
            return
        context = self.ws_manager.execution_contexts.context_for(owner)
        if context is None:
            context = self.ws_manager.load_conversation_execution(owner, self)
        context.thread_runtime.command_prompt_allow_rules = rules

    def permission_context_for_conversation(
        self,
        conversation: Any | None,
        *,
        source: str,
    ):
        requested = (
            normalize_permission_mode(str(getattr(conversation, "permission_mode", DEFAULT_CONVERSATION_PERMISSION_MODE)))
            or DEFAULT_CONVERSATION_PERMISSION_MODE
        )
        requested, approval_policy, sandbox_mode, requirements_source = _managed_permission_projection(
            requested, self.config.permissions.approval_policy, config_stack=self.config.config_layer_stack
        )
        deny_rules = normalize_tool_patterns(getattr(conversation, "permission_deny_rules", []))
        overrides = normalize_permission_overrides(getattr(conversation, "permission_overrides", {}))
        scope = workspace_scope_for(
            workspace_root=getattr(conversation, "workspace_root", "") if conversation is not None else "",
            worktree_path=getattr(conversation, "worktree_path", "") if conversation is not None else "",
        )
        plan_path = None
        if requested == "plan" and conversation is not None:
            plan_path = plan_path_for_snapshot(
                getattr(conversation, "context_snapshot", {}) or {},
                getattr(conversation, "workspace_root", "") or None,
            )
        owner = str(getattr(conversation, "id", "") or "").strip()
        base_constraints = self._base_permission_constraints
        if owner and self.permission_context.conversation_id == owner:
            base_constraints = self.permission_context.filesystem_constraints
        constraints = merge_plan_constraints(base_constraints, plan_path)
        previous_mode = str(getattr(conversation, "permission_previous_mode", "") or "").strip()
        return replace(self.permission_checker.build_context(
            mode=requested,
            session_overrides=overrides,
            command_prompt_allow_rules=self._command_prompt_rules_for_conversation(owner),
            tool_deny_rules=deny_rules,
            filesystem_constraints=constraints,
            workspace_scope=scope,
            source=source,
            pre_plan_mode=previous_mode or None,
            approval_policy=approval_policy,
            sandbox_mode=sandbox_mode,
            requirements_source=requirements_source,
        ), conversation_id=owner)

    def _set_permission_context(
        self,
        *,
        mode: str | None = None,
        session_overrides: dict[str, PermissionLevel] | None = None,
        command_prompt_allow_rules: tuple[str, ...] | list[str] | None = None,
        tool_deny_rules: list[str] | None = None,
        source: str,
    ) -> bool:
        current = self.permission_context
        normalized_mode = (
            normalize_permission_mode(mode if mode is not None else current.mode)
            or DEFAULT_CONVERSATION_PERMISSION_MODE
        )
        normalized_mode, approval_policy, sandbox_mode, requirements_source = _managed_permission_projection(
            normalized_mode, self.config.permissions.approval_policy, config_stack=self.config.config_layer_stack
        )
        normalized_overrides = dict(session_overrides if session_overrides is not None else current.session_overrides)
        normalized_prompt_rules = tuple(
            dict.fromkeys(
                prompt
                for prompt in (
                    str(item or "").strip()
                    for item in (
                        command_prompt_allow_rules
                        if command_prompt_allow_rules is not None
                        else current.command_prompt_allow_rules
                    )
                )
                if prompt
            )
        )
        normalized_deny_rules = list(tool_deny_rules if tool_deny_rules is not None else current.tool_deny_rules)

        if (
            current.mode == normalized_mode
            and current.session_overrides == normalized_overrides
            and current.command_prompt_allow_rules == normalized_prompt_rules
            and current.tool_deny_rules == normalized_deny_rules
            and current.source == source
            and current.approval_policy_value() == approval_policy
            and current.sandbox_mode == sandbox_mode
            and current.requirements_source == requirements_source
        ):
            return False

        self.permission_context = replace(self.permission_checker.build_context(
            mode=normalized_mode,
            session_overrides=normalized_overrides,
            command_prompt_allow_rules=normalized_prompt_rules,
            tool_deny_rules=normalized_deny_rules,
            filesystem_constraints=current.filesystem_constraints,
            workspace_scope=getattr(current, "workspace_scope", "project"),
            source=source,
            pre_plan_mode=(
                current.mode
                if normalized_mode == "plan" and current.mode != "plan"
                else current.pre_plan_mode if normalized_mode == "plan" else None
            ),
            approval_policy=approval_policy,
            sandbox_mode=sandbox_mode,
            requirements_source=requirements_source,
        ), conversation_id=str(self.active_conversation_id or ""))
        self.session_lifecycle.schedule_runtime_capabilities(source=f"permission.{source}")
        return True

    def set_permission_context_mode(self, mode: str, *, source: str) -> bool:
        return self._set_permission_context(mode=mode, source=source)

    def set_permission_context_rules(
        self,
        *,
        session_overrides: dict[str, PermissionLevel],
        tool_deny_rules: list[str],
        source: str,
    ) -> bool:
        return self._set_permission_context(
            session_overrides=session_overrides,
            tool_deny_rules=tool_deny_rules,
            source=source,
        )

    def _add_command_prompt_allow_rules(
        self,
        prompts: list[str] | tuple[str, ...],
        *,
        source: str,
        conversation_id: str | None = None,
    ) -> bool:
        owner = str(conversation_id or self.active_conversation_id or "").strip()
        current = self._command_prompt_rules_for_conversation(owner)
        merged = tuple(
            dict.fromkeys(
                [
                    *current,
                    *(
                        prompt
                        for prompt in (str(item or "").strip() for item in prompts)
                        if prompt
                    ),
                ]
            )
        )
        if merged == current:
            return False
        self._set_command_prompt_rules_for_conversation(owner, merged)
        if owner == str(self.active_conversation_id or ""):
            self._set_permission_context(command_prompt_allow_rules=merged, source=source)
        return True

    async def add_command_prompt_rules_for_conversation(
        self, conversation_id: str, prompts: list[str] | tuple[str, ...], *, source: str,
    ) -> None:
        if not self._add_command_prompt_allow_rules(prompts, source=source, conversation_id=conversation_id):
            return
        from backend.ws.conversation_activity import live_sessions

        rules = self._command_prompt_rules_for_conversation(conversation_id)
        for target in live_sessions(self):
            if target.active_conversation_id != conversation_id:
                continue
            if target is not self:
                target._set_permission_context(command_prompt_allow_rules=rules, source=source)
            await target.emit_permission_rules_updated(conversation_id=conversation_id, source=source)

    def sync_permission_mode_with_active_conversation(self, *, source: str) -> str:
        active = self.active_conversation
        self.permission_context = self.permission_context_for_conversation(active, source=source)
        self.session_lifecycle.schedule_runtime_capabilities(source=f"workspace.activate.{source}")
        return self.permission_context.mode

    def build_permission_rules_payload(self, *, conversation: Any | None = None) -> dict[str, Any]:
        mode = self.permission_context.mode
        context_source = self.permission_context.source
        deny_rules = list(self.permission_context.tool_deny_rules)
        overrides = dict(self.permission_context.session_overrides)
        target_id = str(getattr(conversation, "id", "") or self.active_conversation_id or "").strip()
        prompt_rules = list(self._command_prompt_rules_for_conversation(target_id))

        if conversation is not None:
            conversation_id = str(getattr(conversation, "id", "")).strip()
            if conversation_id and conversation_id != str(self.active_conversation_id or ""):
                mode = (
                    normalize_permission_mode(str(getattr(conversation, "permission_mode", DEFAULT_CONVERSATION_PERMISSION_MODE)))
                    or DEFAULT_CONVERSATION_PERMISSION_MODE
                )
                mode, _approval_policy, _sandbox_mode, _requirements_source = _managed_permission_projection(
                    mode, self.config.permissions.approval_policy, config_stack=self.config.config_layer_stack
                )
                context_source = "conversation.record"
                deny_rules = normalize_tool_patterns(getattr(conversation, "permission_deny_rules", []))
                overrides = normalize_permission_overrides(getattr(conversation, "permission_overrides", {}))

        policy_snapshot = self.permission_checker.policy_snapshot()
        system_deny = normalize_tool_patterns(policy_snapshot.get("always_deny", []))

        return {
            "mode": mode,
            "context_source": context_source,
            "system_deny": [
                {"pattern": pattern, "source": "system.always_deny"}
                for pattern in system_deny
            ],
            "session_deny": [
                {"pattern": pattern, "source": "conversation.runtime"}
                for pattern in deny_rules
            ],
            "session_overrides": [
                {
                    "pattern": pattern,
                    "level": permission_level_to_token(level),
                    "source": "conversation.runtime",
                }
                for pattern, level in sorted(overrides.items(), key=lambda item: item[0])
            ],
            "session_prompt_rules": [
                {
                    "tool": "run_command",
                    "rule_content": f"prompt: {prompt}",
                    "behavior": "allow",
                    "destination": "session",
                    "source": "exit_plan_mode",
                }
                for prompt in prompt_rules
            ],
        }

    async def emit_permission_mode_updated(self) -> None:
        await self.send_payload(
            {
                "type": "permission.mode.updated",
                "session_id": self.session_id,
                "conversation_id": str(self.active_conversation_id or ""),
                "workspace_root": str(self.session_lifecycle.workspace_root_for_conversation() or ""),
                "mode": self.permission_context.mode,
                "source": self.permission_context.source,
            },
            log_context="permission.mode.updated",
        )

    async def emit_permission_rules_updated(
        self,
        *,
        conversation_id: str | None = None,
        source: str = "websocket.command",
    ) -> None:
        target_id = str(conversation_id or self.active_conversation_id or "").strip()
        target = self.conversation_repo.get_conversation(target_id) if target_id else None
        rules = self.build_permission_rules_payload(conversation=target)
        await self.send_payload(
            {
                "type": "permission.rules.updated",
                "session_id": self.session_id,
                "conversation_id": target_id,
                "source": source,
                "rules": rules,
            },
            log_context="permission.rules.updated",
        )
        from backend.permissions.patterns import legacy_mcp_allow_warnings

        warnings = legacy_mcp_allow_warnings([
            *self.permission_checker.policy_snapshot().get("auto_allow", []),
            *(rule["pattern"] for rule in rules["session_overrides"] if rule.get("level") == "auto"),
        ])
        if warnings:
            await self.emit_command_result(
                "permissions.rules.legacy",
                "\n".join(warnings),
                level="warning",
                data={"conversation_id": target_id},
            )

    async def emit_command_result(
        self,
        command: str,
        message: str,
        *,
        level: str = "info",
        title: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        await self.send_event(
            AgentEvent.command_result(
                command,
                message,
                level=level,
                title=title,
                data=data,
            )
        )
