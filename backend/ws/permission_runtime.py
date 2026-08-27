from __future__ import annotations

from typing import Any

from backend.agent.message import AgentEvent
from backend.conversations.models import DEFAULT_CONVERSATION_PERMISSION_MODE
from backend.permissions.profiles import workspace_scope_for
from backend.agent.plans import merge_plan_constraints, plan_path_for_snapshot
from backend.tools.base import PermissionLevel
from backend.ws.utils import (
    normalize_permission_mode,
    normalize_permission_overrides,
    normalize_tool_patterns,
    permission_level_to_token,
        )
def _managed_permission_projection(mode: str, source: str) -> tuple[str, str, str, str]:
    del source
    from backend.config import get_config_requirements

    requirements = get_config_requirements()
    effective_mode, violation = requirements.resolve_permission_mode(mode)
    if violation is not None:
        raise violation
    requirement_source = requirements.source_for("allowed_approval_policies") or requirements.source_for(
        "allowed_sandbox_modes"
    )
    return (
        effective_mode,
        requirements.approval_policy_for_mode(effective_mode),
        requirements.sandbox_mode_for_permission_mode(effective_mode),
        str((violation.source if violation is not None else None) or requirement_source or ""),
    )


class SessionPermissionRuntimeMixin:
    def _permission_context_for_conversation(
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
            requested, source
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
        constraints = merge_plan_constraints(
            getattr(self.permission_context, "filesystem_constraints", {}),
            plan_path,
        )
        previous_mode = str(getattr(conversation, "permission_previous_mode", "") or "").strip()
        return self.permission_checker.build_context(
            mode=requested,
            session_overrides=overrides,
            command_prompt_allow_rules=tuple(
                getattr(self.permission_context, "command_prompt_allow_rules", ())
            ),
            tool_deny_rules=deny_rules,
            filesystem_constraints=constraints,
            workspace_scope=scope,
            source=source,
            pre_plan_mode=previous_mode or None,
            approval_policy=approval_policy,
            sandbox_mode=sandbox_mode,
            requirements_source=requirements_source,
        )

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
            normalized_mode, source
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
            and current.approval_policy == approval_policy
            and current.sandbox_mode == sandbox_mode
            and current.requirements_source == requirements_source
        ):
            return False

        self.permission_context = self.permission_checker.build_context(
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
        )
        return True

    def _set_permission_context_mode(self, mode: str, *, source: str) -> bool:
        return self._set_permission_context(mode=mode, source=source)

    def _set_permission_context_rules(
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
    ) -> bool:
        current = tuple(getattr(self.permission_context, "command_prompt_allow_rules", ()))
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
        return self._set_permission_context(
            command_prompt_allow_rules=merged,
            source=source,
        )

    def _sync_permission_mode_with_active_conversation(self, *, source: str) -> str:
        active = self.active_conversation
        requested = (
            normalize_permission_mode(str(getattr(active, "permission_mode", DEFAULT_CONVERSATION_PERMISSION_MODE)))
            or DEFAULT_CONVERSATION_PERMISSION_MODE
        )
        self.permission_context = self._permission_context_for_conversation(active, source=source)
        return self.permission_context.mode

    def _build_permission_rules_payload(self, *, conversation: Any | None = None) -> dict[str, Any]:
        mode = self.permission_context.mode
        context_source = self.permission_context.source
        deny_rules = list(self.permission_context.tool_deny_rules)
        overrides = dict(self.permission_context.session_overrides)
        prompt_rules = list(getattr(self.permission_context, "command_prompt_allow_rules", ()))

        if conversation is not None:
            conversation_id = str(getattr(conversation, "id", "")).strip()
            if conversation_id and conversation_id != str(self.active_conversation_id or ""):
                mode = (
                    normalize_permission_mode(str(getattr(conversation, "permission_mode", DEFAULT_CONVERSATION_PERMISSION_MODE)))
                    or DEFAULT_CONVERSATION_PERMISSION_MODE
                )
                mode, _approval_policy, _sandbox_mode, _requirements_source = _managed_permission_projection(
                    mode, context_source
                )
                context_source = "conversation.record"
                deny_rules = normalize_tool_patterns(getattr(conversation, "permission_deny_rules", []))
                overrides = normalize_permission_overrides(getattr(conversation, "permission_overrides", {}))
                prompt_rules = []

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

    async def _emit_permission_mode_updated(self) -> None:
        await self._send_ws_payload(
            {
                "type": "permission.mode.updated",
                "session_id": self.session_id,
                "mode": self.permission_context.mode,
                "source": self.permission_context.source,
            },
            log_context="permission.mode.updated",
        )

    async def _emit_permission_rules_updated(
        self,
        *,
        conversation_id: str | None = None,
        source: str = "websocket.command",
    ) -> None:
        target_id = str(conversation_id or self.active_conversation_id or "").strip()
        target = self.conversation_repo.get_conversation(target_id) if target_id else None
        await self._send_ws_payload(
            {
                "type": "permission.rules.updated",
                "session_id": self.session_id,
                "conversation_id": target_id,
                "source": source,
                "rules": self._build_permission_rules_payload(conversation=target),
            },
            log_context="permission.rules.updated",
        )

    async def _emit_command_result(
        self,
        command: str,
        message: str,
        *,
        level: str = "info",
        title: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        await self._send_event(
            AgentEvent.command_result(
                command,
                message,
                level=level,
                title=title,
                data=data,
            )
        )
