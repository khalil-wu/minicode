from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PreparedTurnSettings:
    """Input settings previewed before admission and persisted with its user item."""

    permission_mode: str | None = None
    permission_revision: int | None = None
    workspace_binding: dict[str, Any] | None = None
    workspace_revision: int | None = None
    execution_cwd: str = ""
    workspace_context: Any | None = None
    legacy_scope_cwd: str = ""
    plan_slug: str = ""
    plan_path: Path | None = None

    def preview(self, conversation: Any) -> Any:
        changes: dict[str, Any] = {}
        if self.workspace_binding is not None:
            changes.update(self.workspace_binding)
        elif not conversation.execution_cwd and self.execution_cwd:
            changes["execution_cwd"] = self.execution_cwd
        if self.permission_mode is not None:
            changes["permission_mode"] = self.permission_mode
            changes["permission_previous_mode"] = (
                conversation.permission_mode if self.permission_mode == "plan" and conversation.permission_mode != "plan"
                else conversation.permission_previous_mode if self.permission_mode == "plan" else ""
            )
        return replace(conversation, **changes) if changes else conversation

    def admission_arguments(self) -> dict[str, Any]:
        return {
            "permission_mode": self.permission_mode,
            "expected_permission_mode_revision": self.permission_revision,
            "workspace_binding": self.workspace_binding,
            "expected_workspace_binding_revision": self.workspace_revision,
            "execution_cwd": self.execution_cwd,
        }
