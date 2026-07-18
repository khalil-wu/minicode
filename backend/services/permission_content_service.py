from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.services.runtime_control_service import CommandOutcome


@dataclass(frozen=True)
class PermissionContentRuleResult:
    outcome: CommandOutcome
    updated_rules: list[dict[str, Any]] | None = None
    should_emit_config_change: bool = False


def add_permission_content_rule(rule: str, *, deny: bool = False) -> PermissionContentRuleResult:
    from backend.config import add_permission_content_rule as save_permission_content_rule

    clean_rule = str(rule or "").strip()
    clean_deny = bool(deny)
    if not clean_rule:
        return PermissionContentRuleResult(
            CommandOutcome(
                "permissions.content_rule.add",
                "Rule is required, e.g. Bash(npm run:*) or Edit(src/**)",
                level="warning",
            )
        )

    try:
        updated = save_permission_content_rule(clean_rule, deny=clean_deny)
    except Exception as exc:
        return PermissionContentRuleResult(
            CommandOutcome(
                "permissions.content_rule.add",
                f"Failed to save rule: {exc}",
                level="error",
            )
        )

    return PermissionContentRuleResult(
        CommandOutcome(
            "permissions.content_rule.add",
            f"{'Denied' if clean_deny else 'Allowed'} rule saved: {clean_rule}",
            level="success",
            data={"rule": clean_rule, "deny": clean_deny, "rules": updated},
        ),
        updated_rules=updated,
        should_emit_config_change=True,
    )
