from __future__ import annotations

from typing import Any

from backend.agent.message import AgentEvent
from backend.tools.base import PermissionLevel
from backend.ws.utils import (
    normalize_permission_mode,
    normalize_permission_overrides,
    normalize_tool_patterns,
    permission_level_to_token,
)


class SessionPermissionRuntimeMixin:
    def _set_permission_context(
        self,
        *,
        mode: str | None = None,
        session_overrides: dict[str, PermissionLevel] | None = None,
        tool_deny_rules: list[str] | None = None,
        source: str,
    ) -> bool:
        current = self.permission_context
        normalized_mode = normalize_permission_mode(mode if mode is not None else current.mode) or "default"
        normalized_overrides = dict(session_overrides if session_overrides is not None else current.session_overrides)
        normalized_deny_rules = list(tool_deny_rules if tool_deny_rules is not None else current.tool_deny_rules)

        if (
            current.mode == normalized_mode
            and current.session_overrides == normalized_overrides
            and current.tool_deny_rules == normalized_deny_rules
            and current.source == source
        ):
            return False

        self.permission_context = self.permission_checker.build_context(
            mode=normalized_mode,
            session_overrides=normalized_overrides,
            tool_deny_rules=normalized_deny_rules,
            filesystem_constraints=current.filesystem_constraints,
            source=source,
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

    def _sync_permission_mode_with_active_conversation(self, *, source: str) -> str:
        active = self.active_conversation
        requested = normalize_permission_mode(str(getattr(active, "permission_mode", "default"))) or "default"
        deny_rules = normalize_tool_patterns(getattr(active, "permission_deny_rules", []))
        overrides = normalize_permission_overrides(getattr(active, "permission_overrides", {}))
        self._set_permission_context(
            mode=requested,
            session_overrides=overrides,
            tool_deny_rules=deny_rules,
            source=source,
        )
        return requested

    def _build_permission_rules_payload(self, *, conversation: Any | None = None) -> dict[str, Any]:
        mode = self.permission_context.mode
        context_source = self.permission_context.source
        deny_rules = list(self.permission_context.tool_deny_rules)
        overrides = dict(self.permission_context.session_overrides)

        if conversation is not None:
            conversation_id = str(getattr(conversation, "id", "")).strip()
            if conversation_id and conversation_id != str(self.active_conversation_id or ""):
                mode = normalize_permission_mode(str(getattr(conversation, "permission_mode", "default"))) or "default"
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

    def _format_permission_rules_command_message(self, rules: dict[str, Any]) -> str:
        return (
            f"Permission rules: mode {rules['mode']} | "
            f"session deny {len(rules['session_deny'])} | "
            f"overrides {len(rules['session_overrides'])} | "
            f"system deny {len(rules['system_deny'])}"
        )
