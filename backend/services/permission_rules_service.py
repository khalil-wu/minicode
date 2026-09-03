from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.services.runtime_control_service import CommandOutcome
from backend.services.command_target import resolve_conversation_target as resolve_permission_rule_target
from backend.tools.base import PermissionLevel
from backend.ws.utils import (
    normalize_permission_level,
    normalize_permission_overrides,
    normalize_tool_patterns,
    permission_level_to_token,
    serialize_permission_overrides,
)


@dataclass(frozen=True)
class PermissionRuleMutation:
    should_update: bool
    deny_rules: list[str]
    overrides: dict[str, PermissionLevel]
    serialized_overrides: dict[str, str]
    outcome: CommandOutcome



def build_permission_rules_list_outcome(conversation_id: str, rules: dict[str, Any]) -> CommandOutcome:
    message = (
        f"Permission rules: mode {rules['mode']} | "
        f"session deny {len(rules['session_deny'])} | "
        f"overrides {len(rules['session_overrides'])} | "
        f"system deny {len(rules['system_deny'])}"
    )
    return CommandOutcome(
        "permissions.rules.list",
        message,
        data={"conversation_id": conversation_id, "rules": rules},
    )


def prepare_permission_rule_add(
    target: Any,
    data: dict[str, Any],
    *,
    conversation_id: str,
) -> PermissionRuleMutation:
    rule_kind = str(data.get("rule_kind") or data.get("kind") or "deny").strip().lower()
    pattern = str(data.get("pattern") or "").strip()
    deny_rules = normalize_tool_patterns(getattr(target, "permission_deny_rules", []))
    overrides = normalize_permission_overrides(getattr(target, "permission_overrides", {}))
    if not pattern:
        return _mutation_no_update(
            deny_rules,
            overrides,
            "permissions.rules.add",
            "Pattern is required. Use /permissions rules add deny <pattern> or /permissions rules add override <pattern> <level>",
            level="warning",
        )

    level = None
    result_level = "success"
    if rule_kind in {"deny", "block"}:
        already_present = pattern in deny_rules
        if not already_present:
            deny_rules.append(pattern)
        result_message = f"Added deny rule: {pattern}"
        if already_present:
            result_level = "info"
            result_message = f"Deny rule already present: {pattern}"
        payload: dict[str, Any] = {"conversation_id": conversation_id, "rule_kind": "deny", "pattern": pattern}
    elif rule_kind in {"override", "level"}:
        level = normalize_permission_level(data.get("level"))
        if level is None:
            return _mutation_no_update(
                deny_rules,
                overrides,
                "permissions.rules.add",
                "Invalid level. Use auto|confirm|diff|deny",
                level="warning",
            )
        previous_level = overrides.get(pattern)
        overrides[pattern] = level
        result_message = f"Added override rule: {pattern} -> {permission_level_to_token(level)}"
        if previous_level == level:
            result_level = "info"
            result_message = f"Override rule already present: {pattern} -> {permission_level_to_token(level)}"
        payload = {
            "conversation_id": conversation_id,
            "rule_kind": "override",
            "pattern": pattern,
            "level": permission_level_to_token(level),
        }
    else:
        return _mutation_no_update(
            deny_rules,
            overrides,
            "permissions.rules.add",
            "Invalid rule kind. Use deny or override",
            level="warning",
        )

    return PermissionRuleMutation(
        should_update=True,
        deny_rules=deny_rules,
        overrides=overrides,
        serialized_overrides=serialize_permission_overrides(overrides),
        outcome=CommandOutcome("permissions.rules.add", result_message, level=result_level, data=payload),
    )


def prepare_permission_rule_remove(
    target: Any,
    data: dict[str, Any],
    *,
    conversation_id: str,
) -> PermissionRuleMutation:
    rule_kind = str(data.get("rule_kind") or data.get("kind") or "deny").strip().lower()
    pattern = str(data.get("pattern") or "").strip()
    deny_rules = normalize_tool_patterns(getattr(target, "permission_deny_rules", []))
    overrides = normalize_permission_overrides(getattr(target, "permission_overrides", {}))
    if not pattern:
        return _mutation_no_update(
            deny_rules,
            overrides,
            "permissions.rules.remove",
            "Pattern is required. Use /permissions rules remove deny <pattern> or /permissions rules remove override <pattern>",
            level="warning",
        )

    if rule_kind in {"deny", "block"}:
        removed = pattern in deny_rules
        deny_rules = [item for item in deny_rules if item != pattern]
        result_message = f"Removed deny rule: {pattern}"
        result_level = "success"
        if not removed:
            result_message = f"No deny rule matched: {pattern}"
            result_level = "info"
        payload = {"conversation_id": conversation_id, "rule_kind": "deny", "pattern": pattern}
    elif rule_kind in {"override", "level"}:
        removed = pattern in overrides
        overrides.pop(pattern, None)
        result_message = f"Removed override rule: {pattern}"
        result_level = "success"
        if not removed:
            result_message = f"No override rule matched: {pattern}"
            result_level = "info"
        payload = {"conversation_id": conversation_id, "rule_kind": "override", "pattern": pattern}
    else:
        return _mutation_no_update(
            deny_rules,
            overrides,
            "permissions.rules.remove",
            "Invalid rule kind. Use deny or override",
            level="warning",
        )

    return PermissionRuleMutation(
        should_update=True,
        deny_rules=deny_rules,
        overrides=overrides,
        serialized_overrides=serialize_permission_overrides(overrides),
        outcome=CommandOutcome("permissions.rules.remove", result_message, level=result_level, data=payload),
    )


def _mutation_no_update(
    deny_rules: list[str],
    overrides: dict[str, PermissionLevel],
    command: str,
    message: str,
    *,
    level: str,
) -> PermissionRuleMutation:
    return PermissionRuleMutation(
        should_update=False,
        deny_rules=deny_rules,
        overrides=overrides,
        serialized_overrides=serialize_permission_overrides(overrides),
        outcome=CommandOutcome(command, message, level=level),
    )
